import json
import logging
import os
import subprocess
import time
from io import BytesIO

import praw
import requests
import schedule
from atproto import Client
from PIL import Image

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)

# Race-specific hashtag
RACE_HASHTAG = "AustrianGP"

# Update credentials section to use environment variables
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = "FormulaDank_to_Bluesky_Bot/1.0"
BLUESKY_EMAIL = os.environ.get("BLUESKY_EMAIL")
BLUESKY_PASSWORD = os.environ.get("BLUESKY_PASSWORD")

# Initialize Reddit client
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT,
)

# Initialize Bluesky client
bluesky = Client()


def login_with_retry(max_attempts=3):
    for attempt in range(max_attempts):
        try:
            bluesky.login(BLUESKY_EMAIL, BLUESKY_PASSWORD)
            logging.info("Successfully logged in to Bluesky")
            return True
        except Exception as e:
            wait_time = 2**attempt  # Exponential backoff: 1, 2, 4 seconds
            logging.info(
                f"Login attempt {attempt + 1} failed, waiting {wait_time} seconds"
            )
            time.sleep(wait_time)

    logging.error("Failed to login after maximum attempts")
    return False


if not login_with_retry():
    sys.exit(1)


def check_video_audio(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        has_audio = bool(result.stdout.strip())
        logging.info(f"Video {video_path} has audio: {has_audio}")
        if has_audio:
            logging.info("Audio stream detected and will be preserved")
        return has_audio
    except subprocess.CalledProcessError:
        logging.error(f"Video {video_path} audio check failed")
        return False


def split_into_thread(text, limit=300):
    hashtag_section = "#f1 #formula1 #memes #" + RACE_HASHTAG
    first_post_limit = limit - len(hashtag_section) - 2

    if len(text) <= first_post_limit:
        return [text]

    chunks = []
    first_chunk = text[:first_post_limit]
    chunks.append(first_chunk)

    remaining_text = text[first_post_limit:]
    while remaining_text:
        if len(remaining_text) <= limit:
            chunks.append(remaining_text)
            break
        chunk = remaining_text[: limit - 3] + "..."
        chunks.append(chunk)
        remaining_text = remaining_text[limit - 3 :]

    return chunks


def create_hashtag_facets(formatted_text):
    facets = []
    text_bytes = formatted_text.encode("utf-8")
    for tag in ["f1", "formula1", "memes", RACE_HASHTAG]:
        tag_with_hash = f"#{tag}"
        tag_pos = formatted_text.find(tag_with_hash)
        if tag_pos != -1:
            byte_start = len(formatted_text[:tag_pos].encode("utf-8"))
            byte_end = len(
                formatted_text[: tag_pos + len(tag_with_hash)].encode("utf-8")
            )
            facets.append(
                {
                    "index": {"byteStart": byte_start, "byteEnd": byte_end},
                    "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
                }
            )
    return facets


def clean_filename(url):
    if isinstance(url, tuple):
        url = url[0]  # Use video URL from tuple
    base_name = url.split("?")[0]
    return os.path.basename(base_name)


def verify_file_size(file_path, max_size_kb=900):
    if not os.path.exists(file_path):
        logging.error(f"File does not exist: {file_path}")
        return False
    size = os.path.getsize(file_path)
    logging.info(f"File size for {file_path}: {size/1024:.2f}KB")
    return size <= max_size_kb * 1024


def get_video_duration(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout)
    except:
        return 30  # fallback duration if cannot determine


def convert_gif_to_mp4(gif_path):
    output_path = f"{gif_path[:-4]}.mp4"
    try:
        cmd = [
            "ffmpeg",
            "-i",
            gif_path,
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if os.path.exists(output_path):
            os.remove(gif_path)
            return output_path
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Error converting GIF to MP4: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


def compress_video(video_path, max_size_kb=900):
    target_size = max_size_kb * 1024
    original_size = os.path.getsize(video_path)
    has_audio = check_video_audio(video_path)
    duration = get_video_duration(video_path)

    # Cap maximum bitrate at 2000k
    target_bitrate = min(2000, int((target_size * 8) / (duration * 1024)))

    logging.info(f"Original size: {original_size/1024:.2f}KB")
    logging.info(f"Target size: {target_size/1024:.2f}KB")
    logging.info(f"Duration: {duration:.2f}s")
    logging.info(f"Target bitrate: {target_bitrate}k")

    if original_size <= target_size:
        return True

    output_path = f"{video_path}_compressed.mp4"
    min_crf = 18
    max_crf = 35
    current_crf = min_crf

    while min_crf <= max_crf:
        try:
            # Base command with video settings
            cmd = [
                "ffmpeg",
                "-i",
                video_path,
                "-c:v",
                "libx264",
                "-crf",
                str(current_crf),
                "-preset",
                "medium",
                "-movflags",
                "+faststart",
                "-b:v",
                f"{target_bitrate}k",
                "-maxrate",
                f"{min(target_bitrate * 1.5, 2000)}k",
                "-bufsize",
                f"{min(target_bitrate * 2, 2000)}k",
            ]

            # Add audio settings only if audio stream exists
            if has_audio:
                cmd.extend(
                    [
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-strict",
                        "experimental",
                        "-map",
                        "0:v",
                        "-map",
                        "0:a",
                    ]
                )
            else:
                cmd.extend(["-an"])  # Explicitly specify no audio

            # Add output path
            cmd.extend(["-y", output_path])

            logging.info(f"Attempting compression with CRF {current_crf}")
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.stderr:
                logging.error(f"FFmpeg stderr: {result.stderr}")

            if os.path.exists(output_path):
                current_size = os.path.getsize(output_path)
                logging.info(f"Compressed size: {current_size/1024:.2f}KB")

                if current_size <= target_size:
                    if current_crf == min_crf or current_size > target_size * 0.9:
                        os.remove(video_path)
                        os.rename(output_path, video_path)
                        return True
                    max_crf = current_crf - 1
                else:
                    min_crf = current_crf + 1

                os.remove(output_path)
            current_crf = (min_crf + max_crf) // 2

        except subprocess.CalledProcessError as e:
            logging.error(f"Error compressing video: {e}")
            logging.error(f"FFmpeg error output: {e.stderr}")
            if os.path.exists(output_path):
                os.remove(output_path)
            min_crf = current_crf + 1

    return False


def compress_image(image_path, max_size_kb=900):
    img = Image.open(image_path)

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    quality = 95
    scale = 1.0

    while True:
        if scale < 1.0:
            new_size = tuple(int(dim * scale) for dim in img.size)
            resized_img = img.resize(new_size, Image.Resampling.LANCZOS)
        else:
            resized_img = img

        img_byte_arr = BytesIO()
        resized_img.save(img_byte_arr, format="JPEG", quality=quality, optimize=True)

        if len(img_byte_arr.getvalue()) <= max_size_kb * 1024:
            break

        if quality > 20:
            quality -= 5
        else:
            scale *= 0.75

        if scale < 0.3:
            break

    with open(image_path, "wb") as f:
        f.write(img_byte_arr.getvalue())

    return os.path.getsize(image_path) <= max_size_kb * 1024


def create_bluesky_thread(title, media_paths, author):
    logging.info(f"Creating Bluesky thread with title: {title}")
    logging.info(f"Media paths: {media_paths}")

    try:
        root_uri = None
        parent_uri = None
        quoted_text = f'"{title}"\n\n- u/{author}'
        text_chunks = split_into_thread(quoted_text)
        formatted_text = f"{text_chunks[0]}\n\n#f1 #formula1 #memes #{RACE_HASHTAG}"
        facets = create_hashtag_facets(formatted_text)

        image_paths = [
            p
            for p in media_paths
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
        video_paths = [p for p in media_paths if p.lower().endswith((".mp4", ".gif"))]

        if image_paths:
            for i in range(0, len(image_paths), 4):
                chunk = image_paths[i : i + 4]
                images = {"$type": "app.bsky.embed.images", "images": []}

                for media_path in chunk:
                    if not verify_file_size(media_path):
                        if not compress_image(media_path):
                            logging.error(f"Failed to compress image: {media_path}")
                            continue

                    with open(media_path, "rb") as f:
                        image_data = f.read()

                    try:
                        response = bluesky.upload_blob(image_data)
                        if response and response.blob:
                            images["images"].append(
                                {"image": response.blob, "alt": text_chunks[0][:300]}
                            )
                            logging.info(f"Successfully uploaded image: {media_path}")
                        else:
                            logging.error(f"Failed to upload image: {media_path}")
                    except Exception as e:
                        logging.error(f"Error uploading image {media_path}: {str(e)}")
                        continue

                if not images["images"]:
                    logging.error(
                        "No images were successfully processed for this chunk"
                    )
                    continue

                post_text = (
                    formatted_text
                    if i == 0
                    else f"Continued... ({i//4 + 1}/{(len(image_paths) + 3)//4})"
                )

                try:
                    reply_ref = None
                    if root_uri and parent_uri:
                        reply_ref = {
                            "root": {"uri": root_uri, "cid": root_uri.split("/")[-1]},
                            "parent": {
                                "uri": parent_uri,
                                "cid": parent_uri.split("/")[-1],
                            },
                        }

                    post_result = bluesky.send_post(
                        text=post_text,
                        facets=facets if i == 0 else None,
                        embed=images,
                        reply_to=reply_ref if root_uri and parent_uri else None,
                    )

                    if post_result:
                        if not root_uri:
                            root_uri = post_result.uri
                        parent_uri = post_result.uri
                        logging.info(f"Successfully posted image chunk {i//4 + 1}")
                    else:
                        logging.error("Failed to create post with images")
                except Exception as e:
                    logging.error(f"Error creating post: {str(e)}")
                    continue

        for video_path in video_paths:
            if video_path.endswith(".gif"):
                mp4_path = convert_gif_to_mp4(video_path)
                if mp4_path:
                    video_path = mp4_path

            check_video_audio(video_path)
            if not verify_file_size(video_path):
                if not compress_video(video_path):
                    continue

            with open(video_path, "rb") as f:
                video_data = f.read()

            post_text = formatted_text if not root_uri else f"{text_chunks[0]} (Video)"

            reply_ref = None
            if root_uri and parent_uri:
                reply_ref = {
                    "root": {"uri": root_uri, "cid": root_uri.split("/")[-1]},
                    "parent": {"uri": parent_uri, "cid": parent_uri.split("/")[-1]},
                }

            post_result = bluesky.send_video(
                text=post_text,
                video=video_data,
                video_alt=text_chunks[0],
                facets=facets if not root_uri else None,
            )

            if post_result:
                if not root_uri:
                    root_uri = post_result.uri
                parent_uri = post_result.uri

        return True

    except Exception as e:
        logging.error(f"Error creating Bluesky thread: {e}")
        return False


def load_posted_ids():
    if os.path.exists("posted_ids.json"):
        with open("posted_ids.json", "r") as f:
            return set(json.load(f))
    return set()


def save_posted_ids(posted_ids):
    with open("posted_ids.json", "w") as f:
        json.dump(list(posted_ids), f)


def download_media(url, filename):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            if response.status_code == 200:
                with open(filename, "wb") as f:
                    f.write(response.content)

                # Verify file was written successfully
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    logging.info(
                        f"Successfully downloaded {url} to {filename} ({os.path.getsize(filename)} bytes)"
                    )
                    return True
                else:
                    logging.error(
                        f"File download failed - empty or missing file: {filename}"
                    )
                    return False

            logging.error(f"Download failed with status code: {response.status_code}")
            return False

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limit error
                if attempt < max_retries - 1:
                    logging.warning(
                        f"Rate limited, waiting {retry_delay} seconds before retry {attempt + 1}"
                    )
                    time.sleep(retry_delay)
                    continue
            logging.error(f"Failed to download {url}: {str(e)}")

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to download {url}: {str(e)}")

        except IOError as e:
            logging.error(f"Failed to write file {filename}: {str(e)}")

        except Exception as e:
            logging.error(f"Unexpected error downloading {url}: {str(e)}")

    return False


def get_media_urls(post):
    media_urls = []
    logging.info(f"Checking media for post: {post.id}")
    logging.info(f"Post URL: {post.url}")

    try:
        # Handle Imgur links
        if "imgur.com" in post.url:
            if not any(
                post.url.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif"]
            ):
                img_id = post.url.split("/")[-1]
                media_urls.append(f"https://i.imgur.com/{img_id}.png")
                logging.info(f"Converted Imgur URL to direct image: {media_urls[-1]}")
            else:
                media_urls.append(post.url)
                logging.info(f"Added direct Imgur URL: {post.url}")
            return media_urls

        if hasattr(post, "is_gallery") and post.is_gallery:
            logging.info("Post is a gallery")
            if hasattr(post, "media_metadata"):
                for item_id in post.gallery_data["items"]:
                    media_id = item_id["media_id"]
                    if media_id in post.media_metadata:
                        if "p" in post.media_metadata[media_id]:
                            media_url = post.media_metadata[media_id]["p"][0]["u"]
                            media_url = media_url.replace(
                                "preview", "i"
                            )  # Get full resolution
                            media_urls.append(media_url)
                            logging.info(f"Added gallery image: {media_url}")
                        else:
                            logging.warning(
                                f"No preview found for media_id: {media_id}"
                            )
                    else:
                        logging.warning(f"Media metadata not found for ID: {media_id}")

        elif hasattr(post, "url"):
            if "v.redd.it" in post.url and hasattr(post, "media"):
                if post.media and "reddit_video" in post.media:
                    video_url = post.media["reddit_video"]["fallback_url"]
                    base_url = video_url.rsplit("/", 1)[0]

                    # Try multiple possible audio file patterns
                    audio_patterns = [
                        "/DASH_audio.mp4",
                        "/DASH_AUDIO_128.mp4",
                        "/DASH_audio_128.mp4",
                        "/audio",
                        "/DASH_audio",
                        "/DASH_128.mp4",
                        "/DASH_96.mp4",
                        "/DASH_64.mp4",
                    ]

                    # Use the first working audio URL
                    audio_url = None
                    for pattern in audio_patterns:
                        try:
                            test_url = base_url + pattern
                            response = requests.head(test_url, timeout=5)
                            if response.status_code == 200:
                                audio_url = test_url
                                logging.info(f"Found working audio URL: {audio_url}")
                                break
                        except requests.RequestException as e:
                            logging.debug(
                                f"Failed to check audio pattern {pattern}: {str(e)}"
                            )
                            continue

                    if audio_url:
                        logging.info(f"Found audio URL: {audio_url}")
                    else:
                        logging.warning("No audio URL found for video")

                    media_urls.append((video_url, audio_url))
                    logging.info(f"Added video URL: {video_url}")
                else:
                    logging.warning(f"Invalid video data for post: {post.id}")

            elif any(
                post.url.lower().endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp")
            ):
                media_urls.append(post.url)
                logging.info(f"Added direct media URL: {post.url}")
            else:
                logging.info(f"Unsupported media type: {post.url}")

    except Exception as e:
        logging.error(f"Error processing media URLs for post {post.id}: {str(e)}")

    logging.info(f"Total media URLs found: {len(media_urls)}")
    return media_urls


def merge_video_audio(video_path, audio_path, output_path):
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-strict",
        "experimental",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logging.info(f"FFmpeg merge output: {result.stdout}")
        if os.path.exists(output_path):
            return True
        logging.error("Output file not created after merge")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg error: {e.stderr}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error during merge: {str(e)}")
        return False


def download_and_process_media(url, filename):
    logging.info(f"Attempting to download media from URL: {url}")

    if isinstance(url, tuple):  # Video + audio case
        video_url, audio_url = url
        video_path = f"temp_video_{filename}"

        # Download video
        if download_media(video_url, video_path):
            video_size = (
                os.path.getsize(video_path) if os.path.exists(video_path) else 0
            )
            logging.info(f"Video downloaded successfully. Size: {video_size} bytes")

            # Try audio but continue if it fails
            audio_path = f"temp_audio_{filename}"
            has_audio = download_media(audio_url, audio_path)

            if has_audio and os.path.exists(audio_path):
                audio_size = os.path.getsize(audio_path)
                logging.info(f"Audio downloaded successfully. Size: {audio_size} bytes")

                success = merge_video_audio(video_path, audio_path, filename)
                # Cleanup temp files
                for path in [video_path, audio_path]:
                    if os.path.exists(path):
                        os.remove(path)

                if success:
                    final_size = os.path.getsize(filename)
                    logging.info(
                        f"Successfully merged video and audio. Final size: {final_size} bytes"
                    )
                    return True
            else:
                # Use video only when audio isn't available
                logging.info("Processing video without audio")
                os.rename(video_path, filename)
                if os.path.exists(filename):
                    final_size = os.path.getsize(filename)
                    logging.info(
                        f"Successfully saved video only. Final size: {final_size} bytes"
                    )
                    return True
        else:
            logging.error("Video download failed")
            return False

    else:  # Direct media download
        return download_media(url, filename)


def check_and_post():
    logging.info("Starting new check for posts")
    posted_ids = load_posted_ids()
    subreddit = reddit.subreddit("formuladank")
    # Increase time window to catch more posts
    three_hours_ago = time.time() - (1.5 * 3600)

    try:
        for post in subreddit.new(limit=50):  # Increased limit to catch more posts
            if post.created_utc < three_hours_ago:
                continue

            if post.id not in posted_ids:
                logging.info(
                    f"Processing post: {post.title} | ID: {post.id} | URL: {post.url}"
                )

                # Validate post has media content
                if not hasattr(post, "url") or (
                    not any(
                        post.url.lower().endswith(ext)
                        for ext in (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp")
                    )
                    and not hasattr(post, "is_gallery")
                    and "v.redd.it" not in post.url
                ):
                    logging.info(f"Skipping post {post.id} - No supported media found")
                    continue

                media_urls = get_media_urls(post)

                if not media_urls:
                    logging.warning(f"No media URLs found for post {post.id}")
                    continue

                logging.info(f"Found {len(media_urls)} media URLs for post {post.id}")

                media_files = []
                for i, url in enumerate(media_urls):
                    try:
                        clean_url = clean_filename(url)
                        filename = f"temp_{post.id}_{i}{os.path.splitext(clean_url)[1]}"
                        if download_and_process_media(url, filename):
                            if (
                                os.path.exists(filename)
                                and os.path.getsize(filename) > 0
                            ):
                                media_files.append(filename)
                                logging.info(
                                    f"Successfully processed media: {filename}"
                                )
                            else:
                                logging.error(f"Invalid media file: {filename}")
                    except Exception as e:
                        logging.error(f"Error processing media {url}: {str(e)}")
                        continue

                if media_files:
                    try:
                        if create_bluesky_thread(
                            post.title, media_files, post.author.name
                        ):
                            logging.info(
                                f"Successfully posted to Bluesky: {post.title}"
                            )
                            posted_ids.add(post.id)
                            save_posted_ids(posted_ids)
                        else:
                            logging.error(
                                f"Failed to create Bluesky thread for {post.id}"
                            )
                    except Exception as e:
                        logging.error(f"Error creating Bluesky thread: {str(e)}")
                    finally:
                        # Clean up media files
                        for filename in media_files:
                            if os.path.exists(filename):
                                os.remove(filename)
                else:
                    logging.error(f"No valid media files processed for post {post.id}")
    except Exception as e:
        logging.error(f"Error in check_and_post: {str(e)}")


def main():
    check_and_post()


if __name__ == "__main__":
    main()
