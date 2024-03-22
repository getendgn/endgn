import os, requests, json
from flask import Flask, request, jsonify
from pyairtable import Api, Table, Base
from celery import Celery
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone

# Initialize flask app
app = Flask(__name__)

# Environment variables
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
METRICOOL_USER_TOKEN = os.getenv("METRICOOL_USER_TOKEN")
METRICOOL_API_URL = "https://app.metricool.com/api"

# Celery configuration
REDIS_URL = os.getenv("REDIS_URL")
app.config["CELERY_BROKER_URL"] = (
    os.getenv("CELERY_BROKER_URL", REDIS_URL) or "redis://localhost:6379/"
)
app.config["CELERY_RESULT_BACKEND"] = (
    os.getenv("CELERY_RESULT_BACKEND", REDIS_URL) or "redis://localhost:6379/"
)

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
def get_submission_by_id(base_id, submission_id):
    base = Base(api, base_id)
    table = Table(None, base, "Submissions")
    return table.get(submission_id)


def get_platform_strategy(base_id, platform_name):
    base = Base(api, base_id)
    table = Table(None, base, "Strategies and Templates")
    records = table.all(formula=f"{{Platform}} = '{platform_name}'")
    return records[0]["fields"].get("Text") if records else None


def get_platform_prompt(base_id, platform_name):
    base = Base(api, base_id)
    table = Table(None, base, "Prompts")
    records = table.all(formula=f"{{Platform}} = '{platform_name}'")
    return records[0]["fields"].get("Prompt") if records else None


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
        app.logger.error(
            f"Failed to send prompt to Claude. Status code: {response.status_code}"
        )
        raise Exception("Failed to send prompt to Claude.")


def update_response_table(base_id, platform_name, submission_id, response, user_id):
    base = Base(api, base_id)
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
def generate_content_for_platform(platform, base_id, submission_id):
    submission_record = get_submission_by_id(base_id, submission_id)
    strategy_text = get_platform_strategy(base_id, platform)
    prompt_template = get_platform_prompt(base_id, platform)

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

    user_id = submission_record["fields"].get("User", [None])[0]
    api_key = None

    if user_id:
        base = Base(api, base_id)
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
        update_response_table(base_id, platform, submission_id, response, user_id)
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
                args=(platform, AIRTABLE_BASE_ID, submission_id),
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


@app.route("/create-post", methods=["POST"])
def create_post():
    data = request.get_json()
    platform = data.get("platform").lower()
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
        "draft": True,
        "autoPublish": True,
        "shortener": True,
        "autolistData": {"id": list_id},
        "descendants": [],
    }

    if platform == "pinterest":
        scheduled_post_data["pinterestData"] = {"pinNewFormat": True}

    response = post_to_metricool(blog_id, user_id, scheduled_post_data)

    if response.ok:
        return jsonify({"message": "Post created successfully."})
    else:
        app.logger.error("Failed to create post. Request %s", request.data)
        app.logger.error("Response %s", response.content)
        return jsonify({"error": "Failed to create post."}), 500


def post_to_metricool(blog_id, user_id, post_data):
    url = (
        METRICOOL_API_URL
        + f"/v2/scheduler/posts?blogId={blog_id}&userId={user_id}&userToken={METRICOOL_USER_TOKEN}"
    )
    headers = {"Content-Type": "application/json"}
    return requests.post(url, data=json.dumps(post_data), headers=headers)


if __name__ == "__main__":
    app.run()
