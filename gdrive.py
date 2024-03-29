from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from datetime import datetime
from pathlib import Path
import os, requests
import logging

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID")


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
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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
    return None


def create_folder(service, folder_name, parent_id):
    service = get_service()
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    folder_id = get_folder_id(service, parent_id, folder_name)
    if folder_id is None:
        folder = service.files().create(body=file_metadata, fields="id").execute()
        folder_id = folder.get("id")

    return folder_id


def upload_video_to_drive(file_name, video_path, upload_to_path):
    service = get_service()

    parent_folder_id = GDRIVE_ROOT_FOLDER_ID
    for folder_name in upload_to_path.split("/"):
        folder_id = create_folder(service, folder_name, parent_folder_id)
        parent_folder_id = folder_id

    parent_folder_id = create_folder(
        service, datetime.now().strftime("%Y_%m_%d"), parent_folder_id
    )

    media = MediaFileUpload(video_path, resumable=True)
    file_metadata = {"name": file_name, "parents": [parent_folder_id]}
    file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )

    return file.get("id")
