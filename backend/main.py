import base64
import vertexai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from fastapi.responses import StreamingResponse
import asyncio
import re

# --- NEW: Import for Google Cloud Text-to-Speech ---
from google.cloud import texttospeech

# Corrected imports for Vertex AI
from vertexai.generative_models import (
    GenerativeModel, Part, Content, GenerationConfig
)

# --- Pydantic Models ---
class AnalysisRequest(BaseModel):
    document: str
    language: str

class ImageRequest(BaseModel):
    image_data: str

class AnalyzeImageRequest(BaseModel):
    image_data: str
    language: str

class ChatPart(BaseModel):
    text: str

class ChatMessage(BaseModel):
    role: str
    parts: List[ChatPart]

class ChatRequest(BaseModel):
    document: str
    history: List[ChatMessage]
    question: str
    language: str
    
class TTSRequest(BaseModel):
    text: str
    language: str

# --- FastAPI App ---
app = FastAPI()

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Service Initializations ---
PROJECT_ID = "genai-471305"
LOCATION = "us-central1"
vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- NEW: Instantiate Google Cloud Text-to-Speech client ---
texttospeech_client = texttospeech.TextToSpeechClient()

# --- Helper: Reusable Analysis Prompt ---
def get_analysis_prompt(document: str, language: str) -> str:
    """Generates the reusable prompt for document analysis."""
    return f"""
    Act as a friendly personal legal adviser. Your goal is to help a common person understand this document.
    Your response must be in {language}. All explanations must be **very simple, short, and easy to understand.** Avoid legal jargon completely.

    Your response must be in Markdown format and strictly follow this structure:

    First, provide a "### Summary".
    Second, provide a "### Key Clauses Explained".
    Third, provide a "### My Advice To You".

    Under "Key Clauses Explained", list each important clause. For each clause:
    - Start the line with a `*`.
    - Use a 🔴 emoji for high-risk, 🟡 for medium-risk, and 🟢 for safe clauses.
    - Make the clause title bold (e.g., **Ending the Agreement**).
    - After the title, provide a one-sentence explanation of what it means in plain language.

    Under "My Advice To You", give short, practical, bullet-pointed advice that a regular person can easily act on.

    Document:
    ---
    {document}
    ---
    """

# --- API Endpoints ---
@app.get("/")
def read_root():
    return {"message": "LegalEase AI Backend is running"}

@app.post("/analyze-text-stream")
async def analyze_document_stream(request: AnalysisRequest):
    """Analyzes text and streams a structured markdown summary back."""
    async def generate():
        try:
            model = GenerativeModel("gemini-2.5-flash")
            prompt = get_analysis_prompt(request.document, request.language)
            stream = model.generate_content(prompt, stream=True)
            for chunk in stream:
                if chunk.text:
                    yield chunk.text
                    await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Streaming analysis error: {e}")
            yield "An error occurred during analysis."
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/analyze-image-stream")
async def analyze_image_stream(request: AnalyzeImageRequest):
    """Extracts text from an image and immediately streams the analysis."""
    try:
        model_vision = GenerativeModel("gemini-2.5-flash")
        image_bytes = base64.b64decode(request.image_data)
        image_part = Part.from_data(data=image_bytes, mime_type="image/jpeg")
        prompt_parts = [image_part, "Extract all text from this image. Only return the extracted text."]
        ocr_response = model_vision.generate_content(prompt_parts)
        document_text = ocr_response.text

        if not document_text.strip():
            async def no_text_generator():
                yield "### Summary\n\nCould not find any text in the image. Please try another one."
            return StreamingResponse(no_text_generator(), media_type="text/event-stream")
    except Exception as e:
        print(f"Image processing error: {e}")
        async def error_generator():
            yield "### Summary\n\nAn error occurred while processing the image."
        return StreamingResponse(error_generator(), media_type="text/event-stream")

    async def generate_analysis():
        try:
            model_text = GenerativeModel("gemini-2.5-flash")
            prompt = get_analysis_prompt(document_text, request.language)
            stream = model_text.generate_content(prompt, stream=True)
            for chunk in stream:
                if chunk.text:
                    yield chunk.text
                    await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Streaming analysis error: {e}")
            yield "An error occurred during analysis."
    return StreamingResponse(generate_analysis(), media_type="text/event-stream")

@app.post("/chat-with-document")
async def chat_with_document(request: ChatRequest):
    """Handles follow-up questions about a document."""
    async def generate():
        try:
            system_prompt = f"""
            You are a friendly personal legal adviser. Rules:
            - Use short, simple sentences.
            - If the document covers the answer → explain from the document only.
            - If the document does not cover → FIRST say in bold: **This document does not mention that.** Then on the next line, give a short, helpful general explanation.
            - Keep total reply under 4 short sentences. Language: {request.language}
            --- LEGAL DOCUMENT CONTEXT ---
            {request.document}
            ---
            """
            model = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt)
            history_content = [Content(role=msg.role, parts=[Part.from_text(part.text) for part in msg.parts]) for msg in request.history]
            chat = model.start_chat(history=history_content)
            stream = chat.send_message(request.question, stream=True)
            for chunk in stream:
                if chunk.text:
                    yield chunk.text
                    await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Chat streaming error: {e}")
            yield "An error occurred during the chat. Please try again."
    return StreamingResponse(generate(), media_type="text/event-stream")


# --- UPDATED: Text-to-Speech endpoint using Google Cloud TTS API ---
@app.post("/text-to-speech")
async def text_to_speech(request: TTSRequest):
    """Generates speech from text using the Google Cloud Text-to-Speech API."""
    try:
        clean_text = request.text.replace("*", "").strip()

        # Map frontend language names to BCP-47 codes used by the API
        language_mapping = {
            "English": "en-US",
            "Hindi": "hi-IN",
            "Gujarati": "gu-IN",
            "Kannada": "kn-IN",
            "Marathi": "mr-IN",
            "Tamil": "ta-IN",
            "Telugu": "te-IN",
        }
        language_code = language_mapping.get(request.language, "en-US")

        synthesis_input = texttospeech.SynthesisInput(text=clean_text)

        # Select a high-quality WaveNet voice
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            name=f"{language_code}-Wavenet-A" if language_code != "en-US" else "en-US-Studio-O"
        )
        
        # The frontend expects 16-bit PCM audio at a 24000 sample rate
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000
        )

        response = texttospeech_client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )

        audio_base64 = base64.b64encode(response.audio_content).decode("utf-8")
        
        # Construct the mime_type string the frontend expects
        mime_type = f"audio/L16;rate={audio_config.sample_rate_hertz}"

        return {"audio_data": audio_base64, "mime_type": mime_type}

    except Exception as e:
        print(f"TTS Error: {e}")
        raise HTTPException(status_code=500, detail=f"Error generating speech: {e}")

