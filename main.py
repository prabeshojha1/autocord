import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv
import googleapiclient.discovery
from youtube_transcript_api import YouTubeTranscriptApi
import openai

# --- SETUP AND CONFIGURATION ---

# Pull all our secret keys from the .env file.
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Initialize the OpenAI client.
openai.api_key = OPENAI_API_KEY

# This is our simple in-memory "database".
# It stores everything on a per-user basis using their unique Discord ID.
# { 'user_id': { 'subject_name': {'playlist_id': '...', 'cached_lecture': {...}} } }
user_data = {}

# Define the bot's permissions (intents). We need to read messages.
intents = discord.Intents.default()
intents.message_content = True

# Create our discord bot instance. Commands will start with '!'.
bot = commands.Bot(command_prefix='!', intents=intents)

# --- HELPER FUNCTIONS ---

def extract_playlist_id(url):
    """A small helper to grab the playlist ID from a YouTube URL."""
    match = re.search(r"list=([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None

# --- BOT EVENTS ---

@bot.event
async def on_ready():
    """Fires when the bot successfully connects to Discord."""
    print(f'Alright, {bot.user.name} is online and ready to go!')

# --- BOT COMMANDS ---

@bot.command(name='addsubject', help='Adds a new subject for you to track.')
async def add_subject(ctx, subject_name: str):
    """Creates a new subject profile for the user in their DMs."""
    # This bot is designed for DMs. Let's make sure we're in one.
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("Hey, this command only works in our DMs. Keeps things private!")
        return

    user_id = ctx.author.id
    subject_name = subject_name.lower()

    # Make a spot for the user if they're new here.
    if user_id not in user_data:
        user_data[user_id] = {}

    # Check if they've already added this subject.
    if subject_name in user_data[user_id]:
        await ctx.send(f"Looks like you're already tracking '{subject_name}'.")
    else:
        user_data[user_id][subject_name] = {'playlist_id': None, 'cached_lecture': None}
        await ctx.send(f"Cool, I've added '{subject_name}' to your list. Now use `!setplaylist {subject_name} [url]` to give me the YouTube playlist.")

@bot.command(name='setplaylist', help='Links a YouTube playlist to one of your subjects.')
async def set_playlist(ctx, subject_name: str, playlist_url: str):
    """Links a playlist URL to a subject the user is tracking."""
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("Hey, this command only works in our DMs!")
        return

    user_id = ctx.author.id
    subject_name = subject_name.lower()

    # Make sure the subject actually exists first.
    if user_id not in user_data or subject_name not in user_data[user_id]:
        await ctx.send(f"Hmm, I can't find '{subject_name}' in your list. Try adding it first with `!addsubject {subject_name}`.")
        return

    playlist_id = extract_playlist_id(playlist_url)
    if playlist_id:
        user_data[user_id][subject_name]['playlist_id'] = playlist_id
        await ctx.send(f"Got it. Playlist for '{subject_name}' is locked in. You're all set to use `!latestlec`.")
    else:
        await ctx.send("That doesn't look like a valid YouTube playlist URL. Make sure it has `list=` in it and try again.")

@bot.command(name='latestlec', help='Summarizes the latest lecture for one of your subjects.')
async def latest_lecture_summary(ctx, subject_name: str):
    """The main event: fetches, transcribes, and summarizes the latest lecture video."""
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("Hey, this command only works in our DMs!")
        return

    user_id = ctx.author.id
    subject_name = subject_name.lower()

    # First, let's do some checks to make sure everything is set up.
    user_profile = user_data.get(user_id)
    if not user_profile or subject_name not in user_profile or user_profile[subject_name]['playlist_id'] is None:
        await ctx.send(f"Looks like '{subject_name}' isn't fully set up. Make sure you've added the subject and set its playlist first.")
        return
        
    playlist_id = user_profile[subject_name]['playlist_id']
    
    await ctx.send("On it! Checking the playlist for the latest lecture... 🕵️")

    try:
        # STEP 1: Find the latest video using the YouTube API.
        youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        request = youtube.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=1)
        response = request.execute()
        
        if not response['items']:
            await ctx.send("I looked, but that playlist seems to be empty.")
            return
            
        latest_video = response['items'][0]['snippet']
        video_id = latest_video['resourceId']['videoId']
        video_title = latest_video['title']
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        await ctx.send(f"Found it: **{video_title}**. Grabbing the transcript now.")
        
        # STEP 2: Get the video's transcript.
        try:
            ytt_api = YouTubeTranscriptApi()
            transcript_data = ytt_api.fetch(video_id)
            
            if hasattr(transcript_data, 'transcript'):
                transcript_list = transcript_data.transcript
            elif hasattr(transcript_data, 'captions'):
                transcript_list = transcript_data.captions
            else:
                transcript_list = list(transcript_data)
            
            transcript_text = " ".join([item['text'] if isinstance(item, dict) else item.text for item in transcript_list])
            
        except Exception as e:
            print(f"Transcript error for video {video_id}: {type(e).__name__}: {e}")
            print(f"Full error details: {e}")
            await ctx.send(f"Bummer, I couldn't get a transcript for this video. Error: {type(e).__name__}: {str(e)}\n\nHere's the link anyway: {video_url}")
            return
        
        await ctx.send("Transcript acquired. Sending it to the AI brain for summarization. This can take a moment...")

        # STEP 3: Summarize the text with OpenAI.
        prompt = f"Summarize the key points of the following lecture transcript into clear, concise bullet points. Make it easy to digest. Lecture Title: {video_title}\n\nTranscript:\n{transcript_text}"
        
        chat_completion = openai.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
        )
        summary = chat_completion.choices[0].message.content

        # STEP 4: Cache the result for this user and subject.
        user_data[user_id][subject_name]['cached_lecture'] = {
            'transcript': transcript_text,
            'summary': summary,
            'title': video_title
        }

        # STEP 5: Send the final summary back to the user in a nice embed.
        embed = discord.Embed(
            title=f"📝 Here's the TL;DW for: {video_title}",
            description=summary,
            color=discord.Color.from_rgb(114, 137, 218) # Blurple color
        )
        embed.add_field(name="Watch the Full Lecture", value=f"[Click here to watch]({video_url})", inline=False)
        embed.set_footer(text=f"Now you can run !quizme {subject_name} for a quick quiz on this.")
        
        await ctx.send(embed=embed)

    except Exception as e:
        # A general catch-all for any other unexpected problems.
        await ctx.send(f"Whoops, something went wrong on my end. Maybe check the YouTube API key or Playlist ID? Error: {e}")
        print(f"--- An error occurred ---\n{e}\n-------------------------")

# --- Let's get this thing running! ---
bot.run(DISCORD_TOKEN)