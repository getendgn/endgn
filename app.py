from flask import Flask, request, jsonify
import os
import requests
import time
from pyairtable import Api, Table, Base
from celery import Celery

app = Flask(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

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

# Helper functions
api = Api(AIRTABLE_API_KEY)  # Initialize once and use globally


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


def send_prompt_to_claude(prompt, claude_model):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
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
        return None


def update_response_table(base_id, platform_name, submission_id, response):
    base = Base(api, base_id)
    table = Table(None, base, platform_name)
    fields = {"Submission": [submission_id], "Post Body": response}
    table.create(fields)


@celery.task
def generate_content_for_platform(platform, base_id, submission_id, claude_model):
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
    response = send_prompt_to_claude(prompt, claude_model)

    if response:
        update_response_table(base_id, platform, submission_id, response)
        return f"Content generated and saved to Airtable for {platform}"
    else:
        return f"Error generating content for {platform}"


@app.route("/generate-content", methods=["POST"])
def generate_content_route():
    app.logger.info("Received generate-content request")
    data = request.get_json()
    app.logger.info(f"Request data: {data}")

    submission_data = data.get("submission_id")
    claude_model = data.get("claude_model")
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
        for platform in platforms:
            app.logger.info(f"Generating content for platform: {platform}")
            generate_content_for_platform.apply_async(
                args=(platform, AIRTABLE_BASE_ID, submission_id, claude_model)
            )
            time.sleep(5)

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


if __name__ == "__main__":
    app.run()
