import requests, os, time
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
    if not response.ok:
        raise Exception(f"Failed to send prompt. Status: {response.status_code}")

    json_response = response.json()
    task_id = None
    if json_response.get("success"):
        task_id = json_response["task_id"]
    else:
        raise Exception(f"Invalid prompt")

    data = midjourney_refresh(task_id)
    print(data)


def midjourney_refresh(task_id):
    data = {"task_id": task_id}
    fetch_endpoint = "https://api.midjourneyapi.xyz/mj/v2/fetch"
    response = requests.post(fetch_endpoint, json=data)

    retry_delay = 1
    retry_backoff = 4
    max_retries = 10

    for _ in range(max_retries):
        if response.status_code != 200:
            raise Exception(
                f"Failed to fetch Goapi taskid. Status: {response.status_code}"
            )
        status = response.json()["status"]
        if status == "finished":
            return response.json()
        elif status == "failed":
            raise Exception(f"Goapi fetch with taskid returns failed")
        else:
            print(response.json())
            print("Status is {}, retrying in {} seconds...".format(status, retry_delay))
            time.sleep(retry_delay)
            retry_delay *= retry_backoff

    raise Exception("Request timed out after {} retries".format(max_retries))


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
