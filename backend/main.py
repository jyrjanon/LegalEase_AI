import base64
import vertexai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
import re
from google.cloud import texttospeech

from vertexai.generative_models import GenerativeModel, Part, Content, HarmCategory, HarmBlockThreshold

# --- Pydantic Models ---
class AnalysisRequest(BaseModel):
    document: str
    language: str

class ImageRequest(BaseModel):
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

# --- Vertex AI & TTS Initialization ---
PROJECT_ID = "genai-471305"
LOCATION = "us-central1"
vertexai.init(project=PROJECT_ID, location=LOCATION)
tts_client = texttospeech.TextToSpeechClient()

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
            prompt = f"""
            Act as a friendly personal legal adviser. Your goal is to help a common person understand this document.
            Your response must be in {request.language}. All explanations must be **very simple, short, and easy to understand.** Avoid legal jargon completely.
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
            {request.document}
            ---
            """
            stream = model.generate_content(prompt, stream=True)
            for chunk in stream:
                if hasattr(chunk, 'text'):
                    yield chunk.text
                    await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Streaming analysis error: {e}")
            yield "An error occurred during analysis."
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/analyze-image-stream")
async def analyze_image_stream(request: ImageRequest):
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
            prompt = f"""
            Act as a friendly personal legal adviser...
            # (Same prompt as /analyze-text-stream)
            Document:
            ---
            {document_text}
            ---
            """
            stream = model_text.generate_content(prompt, stream=True)
            for chunk in stream:
                if hasattr(chunk, 'text'):
                    yield chunk.text
                    await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Streaming analysis error from image: {e}")
            yield "An error occurred during analysis."
    return StreamingResponse(generate_analysis(), media_type="text/event-stream")

@app.post("/chat-with-document")
async def chat_with_document(request: ChatRequest):
    """Handles follow-up questions about a document using a non-streaming response to prevent duplication."""
    try:
        system_prompt = f"""
        You are a friendly and practical personal legal adviser based in Vadodara, Gujarat, India.
        Your primary goal is to help users understand their legal documents.
        Here are your rules:
        1.  **Prioritize the Document:** If the user's question can be answered from the provided document, base your answer ONLY on the document's content.
        2.  **Use General Knowledge When Needed:** If the document does not contain the answer, use your general knowledge to provide a helpful, concise response.
        3.  **Handle Cost-Related Questions:** When asked about costs (like stamp duty, registration fees, rent agreement charges), provide estimated figures relevant to Gujarat, India. Always state that these are *estimates* and can vary, advising the user to confirm with local authorities.
        4.  **Be Concise:** Keep your answers short and simple, ideally under 4 sentences. Do not repeat sentences.
        5.  **Language:** Respond in {request.language}.
        6.  **Formatting:** If you state the document doesn't contain the information, format it as:
            **This document does not mention that.**
            Then, on the next line, provide your helpful general knowledge answer.
        --- LEGAL DOCUMENT CONTEXT ---
        {request.document}
        ---
        """
        model_with_system_prompt = GenerativeModel("gemini-2.5-flash", system_instruction=system_prompt)
        history_content = [Content(role=msg.role, parts=[Part.from_text(part.text) for part in msg.parts]) for msg in request.history]
        chat = model_with_system_prompt.start_chat(history=history_content)
        
        # --- FIX: Generate the full response, don't stream ---
        response = chat.send_message(request.question, stream=False)
        
        if hasattr(response, 'text'):
            return JSONResponse(content={"response": response.text})
        else:
            # Handle cases where the response might be empty or blocked
            return JSONResponse(content={"response": "I'm sorry, I couldn't generate a response for that."})

    except Exception as e:
        print(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail="An error occurred during the chat.")

@app.post("/text-to-speech")
async def text_to_speech_gcp(request: TTSRequest):
    """Generates speech using Google Cloud Text-to-Speech API."""
    try:
        language_mapping = {
            "English": "en-IN", "Hindi": "hi-IN", "Gujarati": "gu-IN",
            "Kannada": "kn-IN", "Marathi": "mr-IN", "Tamil": "ta-IN", "Telugu": "te-IN",
        }
        lang_code = language_mapping.get(request.language, "en-IN")

        synthesis_input = texttospeech.SynthesisInput(text=request.text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=lang_code,
            ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000
        )
        response = tts_client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        audio_data = base64.b64encode(response.audio_content).decode('utf-8')
        return {"audio_data": audio_data, "mime_type": "audio/L16;rate=24000"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error in text-to-speech: {e}")

