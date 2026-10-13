"""
Phase 3 — pluggable generator for the baseline (non-agentic) RAG endpoint.

Three interchangeable real backends, one interface (`generate(prompt) -> str`), so
baseline_rag.py never cares which is active — plus an offline FakeGenerator for testing the
retrieve -> prompt -> generate -> cite pipeline without any API key or network access.

IMPORTANT: none of the three real backends are testable from a network-restricted sandbox
(OpenRouter, Hugging Face's Inference API, and Gemini's API are all unreachable there) — this
code is correct by construction and follows each provider's documented API, but has only been
exercised here via FakeGenerator. Test the real API calls on your machine, where normal
internet access is available.
"""
import os
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ============================================================
# OpenRouter
# ============================================================

class OpenRouterGenerator:
    """
    OpenRouter LLM generator.

    Supports:
        1. Automatic free-model routing
           model="openrouter/free"

        2. Specific free models
           model="nvidia/nemotron-3-ultra:free"

        3. Automatic fallback between free models

    Requires:
        pip install openai python-dotenv

    Environment:
        OPENROUTER_API_KEY=your_key

    Optional:
        OPENROUTER_MODEL=openrouter/free
    """

    # --------------------------------------------------------
    # Current free models.
    #
    # These can change over time as OpenRouter adds/removes
    # free endpoints.
    # --------------------------------------------------------

    FREE_MODELS = [
        "llama-3.3-70b-instruct:free",
        "nvidia/nemotron-3-ultra:free",
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-super:free",
        "thinking-machines/inkling:free",
        "thinking-machines/inkling-small:free",
        "cohere/north-mini-code:free",
        "qwen/qwen3.8-27b:free",
        "google/gemma-4-26b-a4b-it:free",
        "google/gemma-4-31b-it:free",
    ]

    # --------------------------------------------------------
    # Constructor
    # --------------------------------------------------------

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_retries: int = 2,
    ):
        from openai import OpenAI

        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in your .env file"
            )

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )

        # ----------------------------------------------------
        # Model selection priority:
        #
        # Explicit constructor argument
        #       ↓
        # OPENROUTER_MODEL
        #       ↓
        # openrouter/free
        # ----------------------------------------------------

        self.model = (
            model
            or os.getenv("OPENROUTER_MODEL")
            or "openrouter/free"
        )

        self.temperature = temperature
        self.max_retries = max_retries

    # --------------------------------------------------------
    # Single request
    # --------------------------------------------------------

    def _call_model(
        self,
        model: str,
        prompt: str,
    ) -> str:

        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            temperature=self.temperature,
        )

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError(
                f"Model {model} returned an empty response."
            )

        return content

    # --------------------------------------------------------
    # Generate
    # --------------------------------------------------------

    def generate(self, prompt: str) -> str:

        # ====================================================
        # MODE 1:
        # OpenRouter automatic free-model routing
        # ====================================================

        if self.model == "openrouter/free":

            last_error = None

            for attempt in range(self.max_retries + 1):

                try:

                    return self._call_model(
                        "openrouter/free",
                        prompt,
                    )

                except Exception as e:

                    last_error = e

                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))

            raise RuntimeError(
                "OpenRouter free router failed after "
                f"{self.max_retries + 1} attempts: {last_error}"
            )

        # ====================================================
        # MODE 2:
        # Manual model + fallback
        # ====================================================

        models_to_try = [
            self.model
        ]

        # Add fallback models.
        for model in self.FREE_MODELS:

            if model != self.model:
                models_to_try.append(model)

        last_error = None

        for model in models_to_try:

            try:

                print(f"[OpenRouter] Trying: {model}")

                return self._call_model(
                    model,
                    prompt,
                )

            except Exception as e:

                last_error = e

                print(
                    f"[OpenRouter] Failed: {model}"
                )

                print(
                    f"[OpenRouter] Reason: {e}"
                )

                continue

        raise RuntimeError(
            "All OpenRouter models failed. "
            f"Last error: {last_error}"
        )


# ============================================================
# Hugging Face
# ============================================================

class HuggingFaceGenerator:
    """
    Calls an open-weight model hosted on Hugging Face.

    Requires:
        pip install huggingface_hub

    Environment:
        HF_TOKEN=your_token
    """

    def __init__(
        self,
        model: str = "meta-llama/Llama-3.1-8B-Instruct",
        temperature: float = 0.0,
    ):

        from huggingface_hub import InferenceClient

        token = os.getenv("HF_TOKEN")

        if not token:
            raise RuntimeError(
                "Set HF_TOKEN in your .env file"
            )

        self.client = InferenceClient(
            model=model,
            token=token,
        )

        self.temperature = temperature or 0.01

    def generate(self, prompt: str) -> str:

        response = self.client.chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            temperature=self.temperature,
            max_tokens=1024,
        )

        return response.choices[0].message.content


# ============================================================
# Gemini
# ============================================================

class GeminiGenerator:
    """
    Calls Google's Gemini API.

    Requires:
        pip install google-genai

    Environment:
        GEMINI_API_KEY=your_key
    """

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
    ):

        from google import genai

        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "Set GEMINI_API_KEY in your .env file"
            )

        self.client = genai.Client(
            api_key=api_key
        )

        self.model = model

    def generate(self, prompt: str) -> str:

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
        )

        return response.text


# ============================================================
# Fake Generator
# ============================================================

class FakeGenerator:
    """
    Offline generator used for testing the RAG pipeline.

    No API key.
    No network.
    No real LLM.

    Useful for testing:

        retrieval
             ↓
        prompt construction
             ↓
        generation
             ↓
        citation extraction
    """

    # Phase 6: lets the SQL tool / router recognise an offline run and use canned behaviour
    # instead of expecting this generator to write real SQL or classify a question.
    is_fake = True

    def generate(self, prompt: str) -> str:

        import re

        match = re.search(
            r"--- CONTRACT EXCERPTS ---(.*)--- END EXCERPTS ---",
            prompt,
            re.DOTALL,
        )

        excerpts_block = (
            match.group(1)
            if match
            else prompt
        )

        ids = re.findall(
            r"\[(\d{4}_\w+)\]",
            excerpts_block,
        )

        unique_ids = list(
            dict.fromkeys(ids)
        )[:5]

        cited = (
            ", ".join(
                f"[{i}]"
                for i in unique_ids
            )
            if unique_ids
            else "[no context retrieved]"
        )

        return (
            "[FAKE ANSWER — no real model called] "
            f"Based on the retrieved excerpts {cited}, "
            "a real generator would produce a grounded, "
            "cited answer here."
        )


# ============================================================
# Generator Factory
# ============================================================

def get_generator(
    provider: str,
    model: Optional[str] = None,
):
    """
    Generator factory.

    Examples:

        get_generator("openrouter")

        get_generator(
            "openrouter",
            "nvidia/nemotron-3-ultra:free"
        )

        get_generator(
            "gemini",
            "gemini-2.0-flash"
        )
    """

    if provider == "openrouter":

        return OpenRouterGenerator(
            model=model
        )

    if provider == "huggingface":

        return HuggingFaceGenerator(
            model=model
            or "meta-llama/Llama-3.1-8B-Instruct"
        )

    if provider == "gemini":

        return GeminiGenerator(
            model=model
            or "gemini-3.6-flash"
        )

    if provider == "fake":

        return FakeGenerator()

    raise ValueError(
        f"Unknown provider: {provider!r} "
        "(expected "
        "openrouter/huggingface/gemini/fake)"
    )