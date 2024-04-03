from pydub import AudioSegment
from openai import OpenAI
from logger import logger
import os, random, string


def create_audio_chunks(video_path):
    logger.info("Starting audio chunking")
    chunks_folder = os.path.join(os.getcwd(), "tmp", "chunks")
    os.makedirs(chunks_folder, exist_ok=True)

    audio = AudioSegment.from_file(video_path)

    duration = 60000
    chunk_paths = []
    title = "".join(
        [random.choice(string.ascii_letters + string.digits) for n in range(4)]
    )
    for i in range(len(audio) // duration):
        start_time = i * duration
        end_time = (i + 1) * duration

        path = os.path.join(chunks_folder, f"{title}_{i}.wav")
        segment = audio[start_time:end_time]
        segment.export(path, format="wav")
        chunk_paths.append(path)

    logger.info("Audio chunks created")
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
            full_transcription += transcription

    for i in chunk_paths:
        os.unlink(i)

    logger.info("Transcription completed")
    return full_transcription
