import requests, os
from pathlib import Path


def download_tmp_video(url, file_name):
    response = requests.get(url)

    if not response.ok:
        raise Exception("Failed to download video from video_url")

    Path("tmp").mkdir(parents=True, exist_ok=True)
    file_path = os.path.join("tmp", file_name)

    with open(file_path, "wb") as f:
        f.write(response.content)

    return file_path


def midjourney_imagine(prompt):
    imagine_endpoint = "https://api.midjourneyapi.xyz/mj/v2/imagine"

    headers = {"X-API-KEY": os.getenv("GO_API_KEY")}
    data = {
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "process_mode": "fast",
        "webhook_endpoint": "",
        "webhook_secret": "",
    }
    response = requests.post(imagine_endpoint, json=data, headers=headers)
    print(response.content)

    if not response.ok:
        raise Exception(f"Failed to send prompt. Status: {response.status_code}")

    return response.json()


def dalle2_imagine(prompt):
    dalle2_endpoint = "https://api.openai.com/v1/images/generations"

    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json",
    }
    data = {"prompt": prompt, "model": "dall-e-3", "n": 1, "size": "1024x1024"}
    response = requests.post(dalle2_endpoint, json=data, headers=headers)

    if not response.ok:
        raise Exception(f"Failed to send prompt. Status: {response.status_code}")

    return response.json()


def send_prompt_to_claude(prompt, claude_model, api_key):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data_payload = {
        "model": claude_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.7,
    }
    response = requests.post(
        "https://api.anthropic.com/v1/messages", json=data_payload, headers=headers
    )
    if response.status_code == 200:
        return response.json()["content"][0]["text"].strip()
    else:
        raise Exception(
            f"Failed to send prompt to Claude. Status: {response.status_code}"
        )
