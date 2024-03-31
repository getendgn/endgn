import os, re, json
from flask import Flask, request, jsonify, url_for, session, redirect
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
    delete_file_from_drive,
)
from transcription import transcribe_video
from utils import (
    download_tmp_video,
    download_tmp_image,
    midjourney_imagine,
    send_prompt_to_claude,
    edit_hook_to_image,
    upload_image,
)
from logger import logger
from youtube import flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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


# Helper functions
def get_submission_by_id(submission_id):
    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Submissions")
    return table.get(submission_id)


def get_platform_strategy(platform_name, user_id):
    record = get_user_record(user_id)
    platform_name.replace("LinkedIn Articles", "LinkedIn").replace("Blogs", "Blog")
    field_name = f"{platform_name} Strategy"
    return record["fields"].get(field_name)


def get_platform_prompt(platform_name, user_id):
    record = get_user_record(user_id)
    platform_name.replace("LinkedIn Articles", "LinkedIn").replace("Blogs", "Blog")
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
    submission_record = get_submission_by_id(submission_id)

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

    if submission_data:
        submission_id = submission_data.get("submissionId")
        app.logger.info(f"Submission ID: {submission_id}")

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
    else:
        app.logger.error("Missing submission ID")
        return jsonify({"error": "Missing submission ID."}), 400


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

    response = create_metricool_list_post(blog_id, user_id, list_id)

    if response.status_code != 200:
        app.logger.error("Failed to create list post.")
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
    os.unlink(video_path)

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

    prompt = f"""Generate a YouTube title, description and a very short engaging hook for thumbnail using the provided transcription in JSON format:
    Transcript: "{transcription}"
    You should Speak from first-person perspective and Your response should only include the title, description and hook in JSON format, without any additional information.
    """

    response = send_prompt_to_claude(prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)
    json_response = json.loads(response)
    title = json_response.get("title")
    description = json_response.get("description")
    hook = json_response.get("hook")

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
    logger.info(f"image url: {img_url}")
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

    return "Authorization successful!"


@app.route("/upload-to-youtube", methods=["POST"])
def upload_to_youtube():
    data = request.get_json()
    video_record_id = data.get("record_id")
    user_record_id = data.get("user_record_id")

    base = Base(api, AIRTABLE_BASE_ID)
    table = Table(None, base, "Videos")
    video_record = table.get(video_record_id)

    table = Table(None, base, "Users")
    user_record = table.get(user_record_id)
    token_json_str = user_record["fields"].get("Youtube Credential")
    token_json = json.loads(token_json_str)

    credentials = Credentials.from_authorized_user_info(
        token_json, scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=credentials)

    title = video_record["fields"].get("Video Title")
    description = video_record["fields"].get("Video Description")
    google_drive_url = video_record["fields"].get("Storage Link")
    thumbnail_url = video_record["fields"].get("Thumbnail Image")[0].get("url")

    file_id = re.search(r"open\?id=([^\&]+)", google_drive_url).group(1)
    video_path = download_file_from_drive(file_id)

    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "categoryId": "22",
                "description": description,
                "title": title,
                "defaultLanguage": "en",
                "defaultAudioLanguage": "en",
                "thumbnails": {"default": {"url": thumbnail_url}},
            },
            "status": {"privacyStatus": "private"},
        },
        media_body=MediaFileUpload(video_path),
    )

    response = request.execute()
    return "Video uploaded successfully!"


if __name__ == "__main__":
    app.run()
