import whisper


def transcribe_video(file_path):
    model = whisper.load_model("base")
    result = model.transcribe(file_path, language="en", fp16=False)

    return result["text"]
