from google_auth_oauthlib.flow import InstalledAppFlow


flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    scopes=["https://www.googleapis.com/auth/youtube.upload"],
    redirect_uri="https://endgn-e8584cd0220b.herokuapp.com/oauth2callback",
)
