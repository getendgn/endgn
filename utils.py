import requests, os, time, textwrap
from pathlib import Path
from PIL import Image, ImageFont, ImageDraw
from logger import logger
import cloudinary.uploader
import cloudinary


def download_tmp_image(url, filename):
    response = requests.get(url)

    if not response.ok:
        raise Exception("Failed to download image from image_url")

    Path("tmp").mkdir(parents=True, exist_ok=True)
    file_path = os.path.join("tmp", f"{filename}.png")

    with open(file_path, "wb") as f:
        f.write(response.content)

    return file_path


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
    upscale_task_id = midjourney_upscale(task_id)
    data = midjourney_refresh(upscale_task_id)

    return data["task_result"].get("image_url")


def midjourney_refresh(task_id):
    data = {"task_id": task_id}
    fetch_endpoint = "https://api.midjourneyapi.xyz/mj/v2/fetch"

    retry_delay = 1
    retry_backoff = 4
    max_retries = 10

    for _ in range(max_retries):
        response = requests.post(fetch_endpoint, json=data)
        if response.status_code != 200:
            raise Exception(
                f"Failed to fetch Goapi taskid. Status: {response.status_code}"
            )
        status = response.json()["status"]
        if status == "finished":
            logger.info("Midjourney refresh completed")
            return response.json()
        elif status == "failed":
            raise Exception(f"Goapi fetch with taskid returns failed")
        else:
            logger.info(f"Status is {status}, retrying in {retry_delay} seconds...")
            time.sleep(retry_delay + 20)
            retry_delay *= retry_backoff

    raise Exception("Request timed out after {} retries".format(max_retries))


def midjourney_upscale(task_id):
    upscale_endpoint = "https://api.midjourneyapi.xyz/mj/v2/upscale"

    headers = {"X-API-KEY": os.getenv("GO_API_KEY")}
    data = {
        "origin_task_id": task_id,
        "index": "1",
        "webhook_endpoint": "",
        "webhook_secret": "",
    }

    response = requests.post(upscale_endpoint, json=data, headers=headers)
    if not response.ok:
        raise Exception(f"Failed to send prompt. Status: {response.status_code}")

    json_response = response.json()
    task_id = None
    if json_response.get("success"):
        task_id = json_response["task_id"]
        return task_id
    else:
        raise Exception(f"Failed to upscale")


def send_prompt_to_claude(prompt, claude_model, api_key, retry_count=5):
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
        logger.info(response.json()["content"][0]["text"])
        return response.json()["content"][0]["text"].strip()
    elif response.status_code in (429, 418) or response.status_code >= 500:
        if retry_count > 0:
            wait_time = (2 ** (5 - retry_count)) * 0.5
            time.sleep(wait_time)
            return send_prompt_to_claude(prompt, claude_model, api_key, retry_count - 1)
        raise Exception(
            f"Failed to send prompt to Claude. Status: {response.status_code}"
        )


def edit_hook_to_image(text, img_path):
    img = Image.open(img_path)
    wrapped_text = textwrap.wrap(text, width=40)
    font_size = 1
    img_fraction = 1.6
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
    breakpoint = img_fraction * img.size[0]
    jumpsize = 75

    while True:
        if font.getlength(text=text) < breakpoint:
            font_size += jumpsize
        else:
            jumpsize = jumpsize // 2
            font_size -= jumpsize
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        if jumpsize <= 1:
            break

    total_height = sum(
        (font.getbbox(line)[-1] - font.getbbox(line)[1]) for line in wrapped_text
    )
    y_offset = img.size[1] - total_height - 80

    for line in wrapped_text:
        left, top, right, bottom = font.getbbox(line)
        width = right - left
        height = bottom - top
        x_offset = (img.size[0] - width) // 2
        bbox = draw.textbbox((x_offset, y_offset), text=line, font=font)
        draw.rectangle(bbox, fill=(0, 0, 0, int(255 * 0.1)))
        draw.text((x_offset, y_offset), line, (255, 255, 255), font=font)
        y_offset += height + 16

    img.save(img_path)
    logger.info("Edited hook to Image")


def upload_image(img_path):
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )
    return cloudinary.uploader.upload(img_path)
