'''
The MIT License (MIT)
Copyright © 2024 Dominic Powers

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''
import runpod
import urllib.request
import zipfile
import requests
import warnings
import boto3
from botocore.config import Config
import random
import string
import hashlib
import datetime
import sys
import os
import torch
from openvoice import se_extractor
from openvoice.api import ToneColorConverter
from melo.api import TTS
import nltk

''' Suppress all warnings (FOR PRODUCTION) '''
warnings.filterwarnings("ignore")

''' Link to network volume to OpenVoice checkpoints (if present)'''
def map_network_volume():

    try:
        # Detect network volume mount point
        if os.path.exists('/runpod-volume'):

            network_volume_path = '/runpod-volume'

        elif os.path.exists('/workspace'):

            network_volume_path = '/workspace'

        else:
            # No network volume
            network_volume_path = None

        if network_volume_path is not None:
            # Ensure the whisper cache directory exists on network volume
            os.makedirs(f'{network_volume_path}/OpenVoice', exist_ok=True)

            # Remove existing .cache directory if it exists and create a symbolic link
            if os.path.islink('/app/checkpoints_v2') or os.path.exists('/app/checkpoints_v2'):
                if os.path.isdir('/app/checkpoints_v2'):
                    shutil.rmtree('/app/checkpoints_v2')

                else:
                    os.remove("/app/checkpoints_v2")

            # Create symlink to connect whisper cache to network volume
            os.symlink(f'{network_volume_path}/OpenVoice', '/app/checkpoints_v2')
        return None, None
    except Exception as e:
        return None, e


''' Check if all required directories exist.'''
def check_directories(base_dir, dirs):
    try:
        for dir in dirs:
            if not os.path.exists(os.path.join(base_dir, dir)):
                return False, None
        return True, None
    except Exception as e:
        return None, e

''' Download the zip file and unzip it into the target directory.'''
def download_and_unzip(url, target_dir, zip_filename):

    try:
        # Download the zip file
        urllib.request.urlretrieve(url, zip_filename)

        # Unzip the file
        with zipfile.ZipFile(zip_filename, 'r') as zip_ref:
            zip_ref.extractall(target_dir)

        # Remove the zip file
        os.remove(zip_filename)

        return None, None

    except Exception as e:
        return None, e

''' Generate a unique timestamped filename '''
def generate_unique_filename(base_title='outputs_v2/OpenVoice', extension='.wav'):
    try:
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        hash_str = hashlib.md5(base_title.encode()).hexdigest()[:6]

        filename = f'{base_title}_{timestamp}_{random_str}_{hash_str}{extension}'

        return filename, None

    except Exception as e:
        return None, e

''' Downloads a file from a URL to a local path. '''
def download_file(url, local_filename):
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(local_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_filename, None
    except Exception as e:
        return None, e

''' Uploads a file to an S3 bucket and makes it publicly readable.
    ENV variables BUCKET_ENDPOINT_URL, BUCKET_ACCESS_KEY_ID, and BUCKET_SECRET_ACCESS_KEY are required '''
def upload_to_s3(local_file, bucket_name, object_name):
    try:
        s3_client = boto3.client('s3',
                                 endpoint_url=os.getenv('BUCKET_ENDPOINT_URL'),
                                 aws_access_key_id=os.getenv('BUCKET_ACCESS_KEY_ID'),
                                 aws_secret_access_key=os.getenv('BUCKET_SECRET_ACCESS_KEY'),
                                 config=Config(signature_version='s3v4'))

        s3_client.upload_file(local_file, bucket_name, object_name, ExtraArgs={'ACL': 'public-read'})

        return f"{os.getenv('BUCKET_ENDPOINT_URL')}/{bucket_name}/{object_name}", None

    except Exception as e:
        return None, e

''' Main function to check directories and download/unzip if necessary.'''
def sync_checkpoints(url, target_dir, zip_filename, required_dirs):

    # Check if the required directories exist
    result, error = check_directories(target_dir, required_dirs)

    if error:
        return None, error

    if result:
        print('[OpenVoice]: Cached models present')
        return None, None

    else:
        print('[OpenVoice]: Loading models into Cache')

        result, error = download_and_unzip(url, target_dir, zip_filename)

        if error:
            return None, error
        else:
            return None, None

''' Call openview model to convert text to speech
    ENV variable BUCKET_NAME can set your bucket name, which defaults to OpenVoive'''
def generate_wav(language, text, reference_speaker='resources/1.wav', speed=1.0):

    try:
        # Prepare defaults
        bucket_name = os.getenv('BUCKET_NAME', 'OpenVoice')
        ckpt_converter = 'checkpoints_v2/converter'

        # Ensure all necessary directories exist
        os.makedirs('checkpoints_v2', exist_ok=True)
        os.makedirs('checkpoints_v2/base_speakers', exist_ok=True)
        os.makedirs('checkpoints_v2/base_speakers/ses', exist_ok=True)
        os.makedirs('checkpoints_v2/converter', exist_ok=True)
        output_dir = 'outputs_v2'
        os.makedirs('outputs_v2', exist_ok=True)
        os.makedirs('tmp', exist_ok=True)
        os.makedirs('processed', exist_ok=True)

        src_path = f'{output_dir}/tmp.wav'

        # Detect and output device type
        device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
        print(f'[OpenVoice]: [device]: {device}')

        # Obtain Tone Color Embedding
        tone_color_converter = ToneColorConverter(f'{ckpt_converter}/config.json', device=device)
        tone_color_converter.load_ckpt(f'{ckpt_converter}/checkpoint.pth')

        print(f'[OpenVoice]: Checking if reference speaker file exists: {reference_speaker}')
        if not os.path.exists(reference_speaker):
            return None, f'Reference speaker file not found: {reference_speaker}'

        # 檢查必要的 checkpoints 檔案是否存在
        required_files = [
            f'{ckpt_converter}/config.json',
            f'{ckpt_converter}/checkpoint.pth'
        ]
        for file_path in required_files:
            if not os.path.exists(file_path):
                return None, f'Required checkpoint file not found: {file_path}'

        target_se, audio_name = se_extractor.get_se(reference_speaker, tone_color_converter, vad=False)

        # Initialize TTS model
        model = TTS(language=language, device=device)
        speaker_ids = model.hps.data.spk2id

        for speaker_key in speaker_ids.keys():
            speaker_id = speaker_ids[speaker_key]
            speaker_key = speaker_key.lower().replace('_', '-')

            source_se = torch.load(f'checkpoints_v2/base_speakers/ses/{speaker_key}.pth', map_location=device)
            model.tts_to_file(text, speaker_id, src_path, speed=speed)
            output_audio_path, error = generate_unique_filename()
            if error:
                return None, error

            # Run the tone color converter
            encode_message = '@MyShell'
            tone_color_converter.convert(
                audio_src_path=src_path,
                src_se=source_se,
                tgt_se=target_se,
                output_path=output_audio_path,
                message=encode_message)

        # Upload audio to S3 bucket
        object_name = os.path.basename(output_audio_path)

        # 檢查是否有 S3 憑證，如果沒有則跳過上傳
        if os.getenv('BUCKET_ENDPOINT_URL') and os.getenv('BUCKET_ACCESS_KEY_ID') and os.getenv('BUCKET_SECRET_ACCESS_KEY'):
            uploaded_url, error = upload_to_s3(output_audio_path, bucket_name, object_name)
            if error:
                return None, error
            return uploaded_url, None
        else:
            # 如果沒有 S3 憑證，將檔案轉換為 base64 編碼
            import base64
            
            try:
                with open(output_audio_path, 'rb') as audio_file:
                    audio_data = audio_file.read()
                    audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                
                print(f'[OpenVoice]: No S3 credentials found, returning base64 encoded audio (size: {len(audio_data)} bytes)')
                return f"data:audio/wav;base64,{audio_base64}", None
            except Exception as e:
                return None, f'Failed to encode audio file: {str(e)}'

    except Exception as e:
        return None, e

''' RunPod Handler function that will be used to process jobs. '''
def handler(job):
    try:
        job_input = job['input']

        # Print startup header
        print(f'[OpenVoice]: Processing job request')

        # Ensure tmp directory exists
        os.makedirs('tmp', exist_ok=True)

        # text option - Text to convert to speech
        """
        OPTION: text 
        DEFAULT VALUE: None (REQUIRED) 
        AVAILABLE OPTIONS: Any text you want to convert to speech
        *Override default value using ENV variable DEFAULT_TEXT 
        """
        text = job_input.get('text', os.getenv('DEFAULT_TEXT', None))
        if not text:
            return {'error': 'Text is required'}

        # language option - Language text is written in
        """
        OPTION: language 
        DEFAULT VALUE: EN 
        AVAILABLE OPTIONS: One of ['EN', 'EN-AU', 'EN-BR', 'EN-INDIA', 'EN-US', 'EN-DEFAULT', ES', 'FR', 'ZH', 'JP', 'KR']
        *Override default value using ENV variable DEFAULT_LANGUAGE 
        """
        language = job_input.get('language', os.getenv('DEFAULT_LANGUAGE', 'EN'))

        # voice_url - URL to an mp3 file with the desired speaker's voice recorded
        """
        OPTION: voice_url 
        DEFAULT VALUE: None (REQUIRED) 
        AVAILABLE OPTIONS: Any valid URL to a short mp3 file with a clear recording of the desired voice
        *Override default value using ENV variable DEFAULT_VOICE_URL 
        """
        voice_url = job_input.get('voice_url', os.getenv('DEFAULT_VOICE_URL', None))
        if not voice_url:
            return {'error': 'Voice URL is required'}

        # speed option - Speed at which to speak text
        """
        OPTION: speed 
        DEFAULT VALUE: 1.0 
        AVAILABLE OPTIONS: Look up the range and place here
        *Override default value using ENV variable DEFAULT_SPEED 
        """
        speed = job_input.get('speed', os.getenv('DEFAULT_SPEED', 1.0))

        print(f'[OpenVoice]: Processing voice file from {voice_url}')
        
        # 檢查是否為本地檔案路徑
        if voice_url.startswith('http://') or voice_url.startswith('https://'):
            # 如果是 URL，下載檔案
            reference_speaker, error = download_file(voice_url, 'tmp/audio.wav')
            if error:
                print(f'ERROR in download_file: {error}')
                return {'error': f'Failed to download voice file: {error}'}
        else:
            # 如果是本地檔案路徑，直接使用
            reference_speaker = voice_url
            if not os.path.exists(reference_speaker):
                print(f'ERROR: Local file not found: {reference_speaker}')
                return {'error': f'Local voice file not found: {reference_speaker}'}
        
        # 檢查檔案是否存在且大小合理
        if not os.path.exists(reference_speaker):
            return {'error': 'Voice file does not exist'}
        
        file_size = os.path.getsize(reference_speaker)
        print(f'[OpenVoice]: Voice file size: {file_size} bytes')
        if file_size < 1000:  # 小於 1KB 的檔案可能有問題
            return {'error': f'Voice file too small: {file_size} bytes'}
        
        print(f'[OpenVoice]: Generating audio with language={language}, text="{text[:50]}...", speed={speed}')
        output_audio_path, error = generate_wav(language=language, text=text, reference_speaker=reference_speaker, speed=speed)
        if error:
            print(f'ERROR in generate_wav: {error}')
            return {'error': f'Failed to generate audio: {error}'}
        else:
            print(f'[OpenVoice]: Successfully generated audio: {output_audio_path}')
            return {
                'output_audio_path': output_audio_path
            }
    except Exception as e:
        print(f'[OpenVoice]: Unexpected error: {e}')
        return {'error': f'Unexpected error: {str(e)}'}

if __name__ == '__main__':

    nltk.download('averaged_perceptron_tagger_eng')

    # Stored model
    url = 'https://myshell-public-repo-host.s3.amazonaws.com/openvoice/checkpoints_v2_0417.zip'
    target_dir = '/app'
    zip_filename = os.path.join(target_dir, 'checkpoints_v2_0417.zip')

    # Define the required directory structure for model
    required_dirs = [
        'checkpoints_v2',
        'checkpoints_v2/base_speakers',
        'checkpoints_v2/base_speakers/ses',
        'checkpoints_v2/converter'
    ]

    # Map network volume if attached
    result, error = map_network_volume()
    if error:
        print(f'[OpenVoice][WARNING]: map_network_volume failed: {error}')

    # Initial load (if needed) to populate network volume with checkpoints
    result, error = sync_checkpoints(url, target_dir, zip_filename, required_dirs)
    if error:
        print(f'[OpenVoice][ERROR]: Failed to download checkpoints: {error}')
        sys.exit(1)

    runpod.serverless.start({'handler': handler})
