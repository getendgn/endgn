import os, requests, re, json
from flask import Flask, request, jsonify
from pyairtable import Api, Table, Base
from celery import Celery
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from metricool import (
    schedule_metricool_post,
    create_metricool_list_post,
    update_metricool_list_post,
)
from gdrive import upload_video_to_drive
from transcription import transcribe_video
from utils import download_tmp_video, midjourney_imagine


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


@celery.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    rate_limit="7/m",
)
def process_video_task(video_url, file_name, customer_name, user_name):
    video_path = download_tmp_video(video_url, file_name)
    gdrive_path = f"{customer_name}/{user_name}"
    # upload_video_to_drive(file_name, video_path, gdrive_path)
    # remove video from airtable
    # update video table with drive link

    # transcript = transcribe_video(video_path)
    os.unlink(video_path)
    # update video table with transcription

    transcript = "Hi there, Aaron's still here. I'm going to show you how to create over a hundred pieces of content based on a video in under five minutes. So looking at this video here, so I recorded this video earlier this morning, this goes for about 20 minutes. I'm going to copy it in to my transcript form here and submit that. What that's going to do is going to pass through to my Trello board, which will take a few minutes to upload to my YouTube creation tabs. So that's creating a title, it's creating an image, it's creating a description for my video. I'm just going to get rid of that one. So while that's happening, I'm going to also upload the video to YouTube so that I'm ready to paste the information that gets populated in here. So just going to wait for that to populate. So what's happening is in Zapier, I've created a flow so that when I submit a form, it goes through and it generates a name, a description, a title, thumbnail for the video. And then it creates all of that and publishes it to my Trello board here. And if I don't like what it creates, then I can just drop it back into Image Fix and it'll redo it again for me. Okay, so it's got a notification on my phone saying, please review your video details. So as you can see here, it has, I'll just move my face out the way. So it's created a image thumbnail for the video. It's created a title, it's created a description. It's got all my links already in there. So all I need to do now is just save this image. I've actually also been able to create a compressed image for automatically to make sure that I'm not going to be trying to upload a video that is to, sorry, upload an image that is too big. So that title is slightly too long, but that's okay. I just call it throbbing the greater economy description. So I'm just going to drop that in here. All right, so it's got all my links there. That's all fun. So in this upside-down, dig deep in the essential skills need to be true success in the greater economy. Yeah, so that's pretty much exactly what I talk about, breaks down what I talk about in here. And now we are also going to grab the thumbnail. Okay, so we've got the thumbnail there. And now I just need to make sure I've got the right playlist. Now I'll start my kids. Next, next, no, no copyright material public, publish now. So what's going to happen now is once this is published, it will then populate in here. Okay, so it's picked up the new post that I created with the video. And so that will begin to create the posts in here. So that might take a few minutes for that to populate, but I will show you as I have, show you that happening live. Okay, well, so while that's happening, I'm also going to go in here and publish this to my podcast. So it distributes it to 11 different platforms. Okay, just do that by disabling it up and I'm going to get to my transistor account. So we'll just grab the title again. We just call it thriving in the crater economy. All right. All right, so I'll then pull the description. And paste it straight in here. And that is ready to publish. Now let's check in. Okay, so that's gone through there. Now I just go and check on the trailer board again. This should start to populate. You may be taking 10 to 15 minutes, but the actual work involved is very minimal. I haven't. I paused the video a few times, but I just wait for things to load, but it's only been under five minutes. Okay, so we can see some posts are being generated as we speak. And so that keeps scrolling down. It's going to create about 33 posts here, which includes Twitter, Pinterest, Facebook, Instagram, and LinkedIn. So what I'll do now is I'll just sort of work through these quickly and show you how we can approve them. So step into the crater economy with the content creation system, tell us your unique voice and audience. Yeah, that's fine. Don't know if you hold you back from inspiring. Yep, that's fine too. I might even, you know, I might have been labels and go Facebook and Instagram. Yeah, I'm happy for those that to go on there. I'll say, paste into Twitter and Pinterest. So what will happen there is that'll get automatically scheduled out for Twitter, Pinterest, Facebook, and Instagram. And I have images created, which I'll show you a bit later. So you'll let, yeah, as you can see, there's loads in here that is just a matter of me going through and checking them. So here's the articles. You know, one for LinkedIn here, which, you know, you can, you can make changes if you need to. It'll generate an image for you as well once you've approved it. Yeah, there's three different articles. Yeah, so it's like, and we can adjust how these are, I'll put together as well. Like these are baseline templates. Cool. So I work through, I won't show on the recording, because you know, it takes, you know, maybe five minutes. That's not that interesting watching we do it, but I will post them in. So just while I'm going through here, I will point out that occasionally, like we are using AI to help generate these occasionally, it'll pick up something that is incorrect. So it is important to not just publish everything, we can just move everything from approval over to have it automatically labeled for Twitter and Pinterest, then it gets scheduled. But you know, I haven't actually lived in New York. So I'll just archive that one, but that's, you know, that only happens occasionally. And that's just, all that means is there's probably a mention of New York in a template somewhere, which I just need to pull out. So I'll, yeah, address that. And I'm always improving this system. So by the time you are watching this, it's probably already been taken care of. Yeah, I mean, these are all really good. So most of these are sort of like Twitter thoughts. Okay, so what I'm doing that I'm also going to drop the link for the YouTube video that I just published into opus clips, which will generate clips for me. You know, we don't want if it processes the whole video. I can filter by keywords, but you know, I'm not going to for this. I just want to make it easy to run through it. And so that'll take, um, how long is it saying 15 minutes? Okay, so whether I don't actually have to do anything else until that 15 minutes is done. So I'm not going to see here and watch that on the video. Okay, so let's open up Hootsuite and check out what's being scheduled. So as you can see here, um, all the Twitter posts are being scheduled there. So I've got it. So it staggers it. Um, we don't want to have everything posting at the same time, but, um, so you can see the Facebook and Instagram have been posted there. Um, everything on Twitter. So it kind of it, it processes through. So it's not just like a block all at the same time. If I get to refreshing the page more and more, uh, Twitter would pop up here. Um, and so yeah, like you really want to be posting to Twitter all the time. Like it's such a, um, a low attention threshold platform that you really need to be, um, engaging a lot and publishing a lot of content in order to get traction. Um, Pinterest gets published at the same time as Twitter. So, uh, as you can see here, this is all stuff that I've been putting on Pinterest. So Pinterest is a bit of a slow burn. Um, I haven't made changes to, um, the template, but we can do that as well. We can publish all kinds of different templates. Um, I've just, for, I guess for this use case, I've just been just doing the text and the background, but there's, um, there's heaps more stuff that you can do with it. You can use like, um, images, uh, all kinds of things, um, like from unsplash for like stock images and, and that kind of thing. So, um, yeah, look, there's, there's so much in there. And it's, it's really just, um, yeah, like allowing it to, to go through and publish everything, uh, which has been automatically scheduled. And so just, uh, you can see, yes, and more has been posted. And so just to reiterate, yes, the, uh, the process takes, uh, maybe sort of 40 minutes, tops, um, but the actual work that I'm doing is like, less than five minutes, like the 40, most of the 40 minutes has taken up with me waiting for, uh, things to, to publish. And so, um, the actual waiting time is, yeah, is the majority of that. So, uh, in the video, I've already published 11 podcasts, um, 30 tweet or 29 tweets, uh, got three articles. I just need to quickly, um, check over and publish the LinkedIn as well. Um, and I'll show you in a moment, uh, the, the different videos that will get automatically published as well. Okay, so, uh, Opus clip has generated, well, like how many heaps. 16 different clips, uh, which I can really easily, uh, just publish, like, it's just a matter of, um, like it automatically creates a description and a title. And yeah, I can just go, publish, publish, publish, publish, publish, and then just keep doing that for, for more videos. And like I just sort of, I can schedule the next one. Let's go to today. Um, how about that? Oops. Let's go six p.m. Yeah, schedule. Schedule that for today. Why is it doing that? For 15, etc. So as you can see, there is, you know, I've, I've basically, I think I started this video, I started recording about maybe half an hour ago and I'd like, I'd finished, uh, the actual content already. So I mean, it's like a 20 minute video that I recorded this morning. As you can see in here, um, I then basically I published so much content based on that video. I produced over 100 pieces of content, um, and scheduled it all. And like the actual total like work for me, having to actually focus on what I'm doing and click through it was like minimal. So like if you're a content creator and you're wanting to get your message out to way more people than you are at the moment and publish content way more often than you are at the moment, then you need to get involved with engine and you need to book a call and we'll have a chat."

    prompt = f"""Generate a YouTube title, description and a very short engaging hook for thumbnail using the provided transcription in JSON format:
    Transcript: "{transcript}"
    You should Speak from first-person perspective and Your response should only include the title, description and hook in JSON format, without any additional information.
    """

    response = send_prompt_to_claude(prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)
    json_response = json.loads(response)
    title = json_response.get("title")
    description = json_response.get("description")
    hook = json_response.get("hook")

    # update airtable
    prompt = f"Write a very detailed prompt for Midjourney to generate 16:9 aspect ratio thumbnail images for youtube video with title {title} and description {description}, Your response should only include the prompt, without any additional information."
    mj_prompt = send_prompt_to_claude(prompt, CLAUDE_MODEL, ANTHROPIC_API_KEY)

    response = midjourney_imagine(mj_prompt)
    print(response)


@app.route("/process-video", methods=["POST"])
def process_video():
    data = request.get_json()
    video_url = data.get("video_url")
    video_filename = data.get("video_filename")
    customer_name = data.get("customer_name")
    user_name = data.get("user_name")

    process_video_task.apply_async(
        args=(video_url, video_filename, customer_name, user_name)
    )
    return jsonify({"message": "Video processing task queued."})


if __name__ == "__main__":
    app.run()
