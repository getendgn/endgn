from pydub import AudioSegment
from openai import OpenAI
from logger import logger
import os


def create_audio_chunks(video_path, max_size=24):
    logger.info("Starting audio chunking")
    audio_path = f"{video_path}.wav"
    filename = os.path.basename(video_path).split(".")[0]
    AudioSegment.from_file(video_path).export(audio_path, format="wav")
    audio_size = os.path.getsize(audio_path) / (1024 * 1024)
    num_chunks = int(audio_size / max_size) + 1

    chunks_folder = os.path.join(os.getcwd(), "tmp", "chunks")
    os.makedirs(chunks_folder, exist_ok=True)

    audio = AudioSegment.from_file(audio_path)

    audio_chunks_path = []
    for i in range(num_chunks):
        start_time = i * len(audio) // num_chunks
        end_time = (i + 1) * len(audio) // num_chunks

        audio_chunk = audio[start_time:end_time]
        path = os.path.join(chunks_folder, f"{filename}_{i}.wav")
        audio_chunks_path.append(path)
        audio_chunk.export(path, format="wav")

    os.unlink(audio_path)
    logger.info("Audio chunks created")
    return audio_chunks_path


def transcribe_video(video_path):
    full_transcription = ""
    chunk_path = create_audio_chunks(video_path)

    client = OpenAI()
    for i, path in enumerate(chunk_path):
        audio_file = open(path, "rb")
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="en",
            response_format="text",
        )
        if transcription:
            full_transcription += transcription

    for i in chunk_path:
        os.unlink(i)

    logger.info("Transcription completed")
    return full_transcription
