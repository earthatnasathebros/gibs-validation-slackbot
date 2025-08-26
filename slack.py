import os
import requests

SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN")
CHANNEL     = os.getenv("SLACK_CHANNEL_ID")

# Example: MODIS Terra True Color for today
date = "2025-08-25"
gibs_url = f"https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/{date}/250m/0/0/0.jpg"

data = {
    "channel": CHANNEL,
    "text": f"🌍 NASA GIBS imagery for {date}",
    "blocks": [
        {
            "type": "image",
            "image_url": gibs_url,
            "alt_text": "NASA GIBS imagery"
        }
    ]
}

requests.post("https://slack.com/api/chat.postMessage",
              headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
              json=data)