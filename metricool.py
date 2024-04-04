import os, requests
from logger import logger



USER_TOKEN = os.getenv("METRICOOL_USER_TOKEN")
API_URL = "https://app.metricool.com/api"


def schedule_metricool_post(blog_id, user_id, post_data):
    url = f"{API_URL}/v2/scheduler/posts"
    params = {"blogId": blog_id, "userId": user_id, "userToken": USER_TOKEN}
    headers = {"Content-Type": "application/json"}

    return requests.post(url, json=post_data, headers=headers, params=params)


def create_metricool_list_post(blog_id, user_id, list_id):
    logger.info(f"Creating metricool list post for blog {blog_id}, user {user_id} and list {list_id}.")
    url = f"{API_URL}/lists/posts/create"
    return requests.get(
        url,
        params={
            "blogId": blog_id,
            "userId": user_id,
            "listid": list_id,
            "position": 0,
        },
    )


def update_metricool_list_post(
    blog_id, user_id, list_id, post_id, post_text, media_urls
):
    url = f"{API_URL}/lists/posts/updatepostlist"
    params = {"blogId": blog_id, "userId": user_id, "userToken": USER_TOKEN}

    payload = {
        "listid": (None, list_id),
        "postid": (None, post_id),
        "text": (None, post_text),
        "pictures": (None, str(media_urls)),
    }

    return requests.post(url, files=payload, params=params)
