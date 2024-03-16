from flask import Flask, request, jsonify
import os
import requests
import time
from dotenv import load_dotenv
from pyairtable import Api, Table, Base

# Load environment variables
load_dotenv()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# Initialize Flask
app = Flask(__name__)

# Custom logger setup
if not app.debug:
    import logging
    handler = logging.FileHandler('app.log')
    handler.setLevel(logging.ERROR)
    app.logger.addHandler(handler)

# Helper functions
api = Api(AIRTABLE_API_KEY)  # Initialize once and use globally

def get_submission_by_id(base_id, submission_id):
    base = Base(api, base_id)
    table = Table(None, base, 'Submissions')
    return table.get(submission_id)

def get_platform_strategy(base_id, platform_name):
    base = Base(api, base_id)
    table = Table(None, base, 'Strategies and Templates')
    records = table.all(formula=f"{{Platform}} = '{platform_name}'")
    return records[0]['fields'].get('Text') if records else None

def get_platform_prompt(base_id, platform_name):
    base = Base(api, base_id)
    table = Table(None, base, 'Prompts')
    records = table.all(formula=f"{{Platform}} = '{platform_name}'")
    return records[0]['fields'].get('Prompt') if records else None

def send_prompt_to_claude(prompt):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    data_payload = {
        "model": CLAUDE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.7
    }
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        json=data_payload,
        headers=headers
    )
    if response.status_code == 200:
        return response.json()['content'][0]['text'].strip()
    else:
        app.logger.error(f"Failed to send prompt to Claude. Status code: {response.status_code}")
        return None

def generate_content_for_platform(platform, base_id, submission_id):
    submission_record = get_submission_by_id(base_id, submission_id)
    strategy_text = get_platform_strategy(base_id, platform)
    prompt_template = get_platform_prompt(base_id, platform)
    
    if not prompt_template or not strategy_text:
        return f"No prompt or strategy found for {platform}"

    prompt = prompt_template.format(
        transcript=submission_record['fields'].get('Transcript', ''),
        writing_style=submission_record['fields'].get('Writing Style', ''),
        strategy=strategy_text
    )
    response = send_prompt_to_claude(prompt)
    
    if response:
        update_response_table(base_id, platform, submission_id, response)
        return f"Content generated and saved to Airtable for {platform}"
    else:
        return f"Error generating content for {platform}"

def update_response_table(base_id, platform_name, submission_id, response):
    base = Base(api, base_id)
    table = Table(None, base, platform_name)
    fields = {"Submission": [submission_id], "Post Body": response}
    table.create(fields)

@app.route('/generate-content', methods=['POST'])
def generate_content_route():
    data = request.get_json()
    submission_id = data.get('submission_id')

    if submission_id:
        if isinstance(submission_id, dict):
            submission_id = submission_id.get('submissionId')

        if not submission_id:
            return jsonify({"error": "Invalid submission ID."}), 400

        platforms = os.getenv("PLATFORMS", "LinkedIn Articles,Twitter,Facebook,Instagram,YouTube,Pinterest,Blogs").split(",")
        for platform in platforms:
            generate_content_for_platform(platform, AIRTABLE_BASE_ID, submission_id)
            time.sleep(1)  # Add a 1-second delay between each API call
        return jsonify({"message": "Content generation completed."})
    else:
        return jsonify({"error": "Missing submission ID."}), 400

def get_latest_submission(base_id):
    base = Base(api, base_id)
    table = Table(None, base, 'Submissions')
    records = table.all(sort=['-Created TimeN'])
    return records[0] if records else None

if __name__ == '__main__':
    app.run()