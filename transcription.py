from pydub import AudioSegment
from pydub.silence import split_on_silence
from datetime import datetime
import os, time, openai

OPENAI_API_KEY = "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"


def openai_client():
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def create_audio_chunks(video_path):
    chunks_folder = "tmp/chunks"
    os.makedirs(chunks_folder, exist_ok=True)
    audio = AudioSegment.from_file(video_path)
    chunks = split_on_silence(
        audio, min_silence_len=1000, silence_thresh=audio.dBFS - 16, keep_silence=200
    )

    target_length = 90 * 1000
    output_chunks = [chunks[0]]

    for chunk in chunks[1:]:
        if len(output_chunks[-1]) < target_length:
            output_chunks[-1] += chunk
        else:
            output_chunks.append(chunk)

    chunks_paths = []
    for i, chunk in enumerate(output_chunks):
        t = (datetime.now() - datetime.now()).seconds
        path = f"{chunks_folder}/{t}_{i}.wav"
        chunk.export(path, format="wav")
        chunks_paths.append(path)
    return chunks_paths


def transcribe_video(video_path):
    transcription = ""
    chunk_paths = create_audio_chunks(video_path)

    client = openai_client()
    for i, chunk_path in enumerate(chunk_paths):
        with open(chunk_path, "rb") as f:
            audio_bytes = f.read()

        response = client.audio.transcriptions.create(
            model="whisper-1", file=audio_bytes
        )
        print(response["text"])
        transcription += response["text"]
        time.sleep(5 * i)

    print(transcription)
