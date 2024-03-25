from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from io import BytesIO
from datetime import datetime
import os, requests


def authenticate():
    credential_path = "credentials.json"
    SCOPES = ["https://www.googleapis.com/auth/drive.file"]

    if os.path.exists(credential_path):
        creds = service_account.Credentials.from_service_account_file(credential_path)
        return creds.with_scopes(SCOPES)
    else:
        raise Exception("No credentials found")


def get_service():
    creds = authenticate()
    return build("drive", "v3", credentials=creds)


def get_folder_id(service, parent_id, folder_name):
    results = (
        service.files()
        .list(
            q=f"name='{folder_name}' and parents='{parent_id}' and mimeType='application/vnd.google-apps.folder'",
            fields="files(id)",
        )
        .execute()
    )
    items = results.get("files", [])
    if items:
        return items[0]["id"]

    return


def create_folder(service, folder_name, parent_id):
    service = get_service()
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    try:
        folder = service.files().create(body=file_metadata, fields="id").execute()
    except HttpError as err:
        if err.resp.status == 409:  # folder already exists
            folder_id = get_folder_id(service, parent_id)
            return folder_id

        raise Exception(f"Failed to create folder: {err}")

    return folder.get("id")


def upload_video_to_drive(url, path):
    service = get_service()

    response = requests.get(url, stream=True)
    if not response.ok:
        raise Exception("Failed to download video from video_url")

    parent_folder_id = "1QgdqIL8rSXopjic4m3HwngVaOqn7H7gL"
    for folder_name in path.split("/"):
        folder_id = create_folder(service, folder_name, parent_folder_id)
        parent_folder_id = folder_id

    folder_id = create_folder(
        service, datetime.now().strftime("%Y_%m_%d"), parent_folder_id
    )

    media = MediaIoBaseUpload(
        BytesIO(response.content),
        mimetype="video/mp4",
    )

    now = datetime.now()
    file_name = now.hour * 3600 + now.minute * 60 + now.second
    file_metadata = {
        "name": f"{file_name}.mp4",
        "parents": [parent_folder_id],
    }

    file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )

    return file.get("id")
