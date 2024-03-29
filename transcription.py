from pydub import AudioSegment
from pydub.silence import split_on_silence
from openai import OpenAI
import os, random, string


def create_audio_chunks(video_path):
    chunks_folder = os.path.join(os.getcwd(), "tmp", "chunks")
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

    chunk_paths = []
    title = "".join(
        [random.choice(string.ascii_letters + string.digits) for n in range(4)]
    )
    for i, chunk in enumerate(output_chunks):
        path = os.path.join(chunks_folder, f"{title}_{i}.wav")
        chunk.export(path, format="wav")
        chunk_paths.append(path)
    return chunk_paths


def transcribe_video(video_path):
    full_transcription = ""
    chunk_paths = create_audio_chunks(video_path)

    client = OpenAI()
    for i, chunk_path in enumerate(chunk_paths):
        audio_file = open(chunk_path, "rb")
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="en",
            response_format="text",
        )
        if transcription:
            print(f"Transcription {i}: {transcription}")
            full_transcription += transcription

    return full_transcription
