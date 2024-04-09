import os, re, json
from flask import Flask, request, jsonify, session, redirect, Response
from pyairtable import Api, Table, Base
from celery import Celery
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from metricool import (
    schedule_metricool_post,
    create_metricool_list_post,
    update_metricool_list_post,
)
from gdrive import (
    upload_video_to_drive,
    download_file_from_drive,
)
from transcription import transcribe_video
from utils import (
    download_tmp_video,
    download_tmp_image,
    midjourney_imagine,
    send_prompt_to_claude,
    edit_hook_to_image,
    upload_image,
    get_table_by_id,
    get_file_content,
)
from logger import logger
from youtube import flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from time import sleep

# Initialize flask app
app = Flask(__name__)

# Environment variables
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")


# Celery configuration
REDIS_URL = os.getenv("REDIS_URL")
app.config["CELERY_BROKER_URL"] = (
    os.getenv("CELERY_BROKER_URL", REDIS_URL) or "redis://localhost:6379/"
)
app.config["result_backend"] = (
    os.getenv("CELERY_RESULT_BACKEND", REDIS_URL) or "redis://localhost:6379/"
)
app.secret_key = "SECRETKEY"

# Initialize Celery
celery = Celery(app.name, broker=app.config["CELERY_BROKER_URL"])
celery.conf.update(app.config)


# Custom logger setup
if not app.debug:
    import logging

    handler = logging.FileHandler("app.log")
    handler.setLevel(logging.ERROR)
    app.logger.addHandler(handler)

# Initialize Airtable API
api = Api(AIRTABLE_API_KEY)


def get_platform_strategy(platform_name, user_id):
    record = get_user_record(user_id)
    platform_name = platform_name.replace("LinkedIn Articles", "LinkedIn").replace(
        "Blogs", "Blog"
    )
    field_name = f"{platform_name} Strategy"
    return record["fields"].get(field_name)


def get_platform_prompt(platform_name, user_id):
    record = get_user_record(user_id)
    platform_name = platform_name.replace("LinkedIn Articles", "LinkedIn").replace(
        "Blogs", "Blog"
    )
    field_name = f"{platform_name} Prompt"
    return record["fields"].get(field_name)


def get_user_record(user_id):
    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Users")
    return table.first(formula=f"{{UserID}} = '{user_id}'")


def update_response_table(platform_name, submission_id, response, user_id):
    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, platform_name)
    fields = {"Submission": [submission_id], "Post Body": response}

    # Add user ID if available
    if user_id:
        fields["User"] = [user_id]
    table.create(fields)


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    rate_limit="7/m",
)
def generate_content_for_platform(platform, submission_id):
    sleep(20)
    submission_record = get_table_by_id(
        "Submissions", submission_id, api, AIRTABLE_BASE_ID
    )

    user_id = submission_record["fields"].get("User", [None])[0]

    strategy_text = get_platform_strategy(platform, user_id)
    prompt_template = get_platform_prompt(platform, user_id)

    if not prompt_template or not strategy_text:
        return f"No prompt or strategy found for {platform}"

    prompt_data = {
        "Transcript": submission_record["fields"].get("Transcript", ""),
        "WritingStyle": submission_record["fields"].get("Writing Style", ""),
        "Strategy": strategy_text,
    }

    transcript_file = submission_record["fields"].get("Topic PDF Upload")
    writing_style_file = submission_record["fields"].get("Writing Style PDF Upload")
    if transcript_file:
        file_url = transcript_file[0].get("url")
        prompt_data["Transcript"] = get_file_content(file_url)
    if writing_style_file:
        file_url = writing_style_file[0].get("url")
        prompt_data["WritingStyle"] = get_file_content(file_url)

    prompt = prompt_template.format().format(**prompt_data)
    claude_model = (
        submission_record["fields"].get("Anthropic Model", CLAUDE_MODEL).strip()
    )

    api_key = None
    if user_id:
        base = Base(api, AIRTABLE_BASE_ID)
        keys_table = Table(None, base, "Keys")
        records = keys_table.all(formula=f"{{Provider}} = 'Anthropic'")
        encrypted_api_key = next(
            (
                rec["fields"].get("Key")
                for rec in records
                if rec["fields"].get("User") == [user_id]
            ),
            None,
        )

        if encrypted_api_key:
            api_key = decrypt_key(encrypted_api_key)

    if not api_key:
        raise Exception("No api key provided.")

    response = send_prompt_to_claude(prompt, claude_model, api_key)
    if response:
        user_id = submission_record["fields"].get("User", [None])[0]
        update_response_table(platform, submission_id, response, user_id)
        return f"Content generated and saved to Airtable for {platform}"
    else:
        return f"Error generating content for {platform}"


@app.route("/generate-content", methods=["POST"])
def generate_content_route():
    app.logger.info("Received generate-content request")
    data = request.get_json()
    app.logger.info(f"Request data: {data}")

    submission_data = data.get("submission_id")
    app.logger.info(f"Submission data: {submission_data}")

    if not submission_data:
        app.logger.error("Missing submission ID")
        return jsonify({"error": "Missing submission ID."}), 400

    submission_id = submission_data.get("submissionId")
    if not submission_id:
        app.logger.error("Invalid submission ID")
        return jsonify({"error": "Invalid submission ID."}), 400

    platforms = os.getenv(
        "PLATFORMS",
        "LinkedIn Articles,Twitter,Facebook,Instagram,YouTube,Pinterest,Blogs",
    ).split(",")
    for i, platform in enumerate(platforms):
        app.logger.info(f"Generating content for platform: {platform}")
        generate_content_for_platform.apply_async(
            countdown=i * 10,
            args=(platform, submission_id),
        )

    app.logger.info("Content generation tasks queued")
    return jsonify({"message": "Content generation tasks queued."})


def get_latest_submission(base_id):
    base = Base(api, base_id)
    table = Table(None, base, "Submissions")
    records = table.all(sort=["-Created Time"])
    return records[0] if records else None


@app.route("/split-out-tweets", methods=["POST"])
def split_out_tweets():
    data = request.get_json()
    twitter_record_id = data.get("twitter_record_id")

    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Twitter")
    record = table.get(twitter_record_id)
    post_body = record["fields"]["Post Body"]

    tweets = re.split(r"\n\n+", post_body.strip())

    for tweet in tweets:
        tweet = re.sub(r"^Tweet\d+: ", "", tweet)
        fields = record["fields"]
        fields = {
            "Title": tweet,
            "Post Body": tweet,
            "Submission": fields.get("Submission"),
            "User": fields.get("User"),
            "Status": "For Approval",
        }
        table.create(fields)

    return jsonify({"message": "Split out tweets successfully"})


@app.route("/encrypt_key", methods=["POST"])
def encrypt_key():
    data = request.get_json()

    cipher_suite = Fernet(ENCRYPTION_KEY)
    api_key = data.get("apiKey")
    encrypted_api_key = cipher_suite.encrypt(api_key.encode())

    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Keys")

    # Update the key in Airtable
    record = table.first(formula=f"{{Key}} = '{api_key}'")
    if record:
        table.update(record.get("id"), {"Key": encrypted_api_key.decode()})

    return jsonify({"message": "Key encrypted successfully."})


def decrypt_key(encrypted_key):
    cipher_suite = Fernet(ENCRYPTION_KEY)
    return cipher_suite.decrypt(encrypted_key.encode()).decode()


@app.route("/schedule-post", methods=["POST"])
def schedule_post():
    data = request.get_json()
    platform = data.get("platform", "").lower()
    blog_id = data.get("blog_id")
    user_id = data.get("user_id")
    list_id = data.get("list_id")
    post_text = data.get("text")
    media_urls = data.get("media_urls")

    scheduled_post_data = {
        "providers": [{"network": platform}],
        "publicationDate": {
            "dateTime": (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "timezone": "Australia/Adelaide",
        },
        "text": post_text,
        "media": media_urls,
        "autoPublish": True,
        "shortener": True,
        "descendants": [],
    }

    if platform == "pinterest":
        scheduled_post_data["pinterestData"] = {"pinNewFormat": True}

    response = schedule_metricool_post(blog_id, user_id, scheduled_post_data)

    if response.ok:
        return jsonify({"message": "Post scheduled successfully."})
    else:
        app.logger.error("Failed to schedule post. Request %s", request.data)
        app.logger.error("Response %s", response.content)
        return jsonify({"error": "Failed to create post."}), 400


@app.route("/post-to-list", methods=["POST"])
def post_to_list():
    data = request.get_json()
    blog_id = data.get("blog_id")
    user_id = data.get("user_id")
    list_id = data.get("list_id")
    post_text = data.get("text")
    media_urls = data.get("media_urls")

    if not user_id or not list_id or not blog_id:
        return jsonify({"error": "Missing required parameters."}), 400

    response = create_metricool_list_post(blog_id, user_id, list_id)

    if response.status_code != 200:
        app.logger.error("Failed to create list post, Status: %s", response.status_code)
        app.logger.error("Error: %s", response.content)
        return jsonify({"error": "Failed to create list post."}), 400

    create_post = response.json()[-1]
    response = update_metricool_list_post(
        blog_id, user_id, list_id, create_post["id"], post_text, media_urls
    )
    if not response.ok:
        app.logger.error("Failed to update list post.")
        return jsonify({"error": "Failed to update list post."}), 400

    return jsonify({"message": "Post added to list successfully."})


def update_airtable_table(table, record_id, data):
    logger.info(f"Updating Airtable table {table} id {record_id} with data {data}")
    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Videos")
    table.update(record_id, data)


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    rate_limit="7/m",
)
def process_video_task(record_id, video_url, file_name, customer_name, user_name):
    video_path = download_tmp_video(video_url, file_name)
    gdrive_path = f"{customer_name}/{user_name}"
    file_id = upload_video_to_drive(file_name, video_path, gdrive_path)

    update_data = {
        "Video File": None,
        "Storage Link": f"https://drive.google.com/open?id={file_id}",
    }
    update_airtable_table("Videos", record_id, update_data)
    logger.info(f"Uploaded video to drive: {file_id}")

    transcription = transcribe_video(video_path)
    update_data = {"Transcription": transcription}
    update_airtable_table("Videos", record_id, update_data)
    logger.info(f"Transcribed video and saved to Airtable")
    os.unlink(video_path)

    video_record = get_table_by_id("Videos", record_id, api, AIRTABLE_BASE_ID)
    user_id = video_record["fields"]["User"][0]
    user = get_user_record(user_id)

    title_prompt = user["fields"].get("Video Title Prompt")
    desc_prompt = user["fields"].get("Video Description Prompt")
    hook_prompt = user["fields"].get("Video Hook Prompt")

    title_prompt = title_prompt.format().format(Transcription=transcription)
    desc_prompt = desc_prompt.format().format(Transcription=transcription)
    hook_prompt = hook_prompt.format().format(Transcription=transcription)

    title = send_prompt_to_claude(title_prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)
    sleep(5)
    description = send_prompt_to_claude(desc_prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)
    sleep(5)
    hook = send_prompt_to_claude(hook_prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)

    # update airtable
    update_data = {
        "Video Title": title,
        "Video Description": description,
        "Video Hook": hook,
    }
    update_airtable_table("Videos", record_id, update_data)
    logger.info("Updated 'Videos' table with Title, Description & Hook")

    prompt = f'Write a very detailed prompt for Midjourney to generate 16:9 aspect ratio thumbnail images for youtube video with title "{title}" and description "{description}", Your response should only include the prompt, without any additional information, just raw text no commands or tweaks'
    mj_prompt = send_prompt_to_claude(prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)

    img_url = midjourney_imagine(mj_prompt)
    img_path = download_tmp_image(img_url, hook[:10])
    edit_hook_to_image(hook, img_path)
    edited_img_url = upload_image(img_path).get("secure_url")

    update_data = {"Thumbnail Image": [{"url": edited_img_url}]}
    update_airtable_table("Videos", record_id, update_data)
    logger.info("Updated 'Videos' table with Thumbnail image")

    update_data = {"Status": "Ready for Review"}
    update_airtable_table("Videos", record_id, update_data)
    logger.info("Completed processing video")


@app.route("/process-video", methods=["POST"])
def process_video():
    data = request.get_json()
    video_url = data.get("video_url")
    video_filename = data.get("video_filename")
    customer_name = data.get("customer_name")
    user_name = data.get("user_name")
    record_id = data.get("record_id")

    process_video_task.apply_async(
        args=(record_id, video_url, video_filename, customer_name, user_name)
    )
    return jsonify({"message": "Video processing task queued."})


@app.route("/authorize-youtube", methods=["GET"])
def authorize_youtube():
    user_record_id = request.args.get("user_record_id")
    session["user_record_id"] = user_record_id
    auth_url, state = flow.authorization_url(prompt="consent")
    session["state"] = state

    return redirect(auth_url)


@app.route("/oauth2callback", methods=["GET"])
def oauth2callback():
    state = session.pop("state", None)
    user_record_id = session.pop("user_record_id", None)

    if state is None or state != request.args.get("state"):
        return "Invalid state parameter", 400

    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials

    update_data = {"Youtube Credential": credentials.to_json()}
    update_airtable_table("Users", user_record_id, update_data)

    oauth_success_page = """<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Authorization Successful</title>
    </head>
    <body>
        <h1>Authorization Successful!</h1>
        <p>You may now close this tab.</p>
    </body>
    </html>"""
    return Response(oauth_success_page, mimetype="text/html")


@app.route("/upload-to-youtube", methods=["POST"])
def upload_to_youtube():
    data = request.get_json()
    video_record_id = data.get("video_record_id")
    user_record_id = data.get("user_record_id")

    base = Base(api, AIRTABLE_BASE_ID)

    table = Table(None, base, "Users")
    user_record = table.get(user_record_id)
    token_json_str = user_record["fields"].get("Youtube Credential")
    token_json = json.loads(token_json_str)

    credentials = Credentials.from_authorized_user_info(
        token_json, scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=credentials)

    video_record = get_table_by_id("Videos", video_record_id, api, AIRTABLE_BASE_ID)
    title = video_record["fields"].get("Video Title")
    description = video_record["fields"].get("Video Description")
    google_drive_url = video_record["fields"].get("Storage Link")
    thumbnail_url = video_record["fields"].get("Thumbnail Image")[0].get("url")
    thumbnail = download_tmp_image(thumbnail_url, title[:10])

    file_id = re.search(r"open\?id=([^\&]+)", google_drive_url).group(1)
    video_path = download_file_from_drive(file_id)

    response = (
        youtube.videos()
        .insert(
            part="snippet,status",
            body={
                "snippet": {
                    "categoryId": "22",
                    "description": description,
                    "title": title,
                    "defaultLanguage": "en",
                    "defaultAudioLanguage": "en",
                },
                "status": {"privacyStatus": "public"},
            },
            media_body=MediaFileUpload(video_path),
        )
        .execute()
    )

    # set thumbnail
    youtube.thumbnails().set(
        video_id=response["id"],
        media_body=thumbnail,
    )
    update_airtable_table(
        "Videos", video_record_id, {"Youtube Video ID": response["id"]}
    )
    logger.info(f"Uploaded video to youtube.")
    return "Video uploaded successfully!"


if __name__ == "__main__":
    app.run()
