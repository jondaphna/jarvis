from dotenv import load_dotenv
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import ai_coustics, google

from brain_memory import JarvisMemory
from browser import BrowserManager
from control_api import serve_in_background
from os_tools import OSTools
from personalise import extra_instructions, load_rules, load_settings
from prompts import AGENT_INSTRUCTIONS
from tools import BrowserTools

load_dotenv(".env.local")


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None,
                 memory: JarvisMemory | None = None) -> None:
        self.browser = browser or BrowserManager(headless=True)
        self.browser_tools = BrowserTools(self.browser)
        # Memory, your custom commands and the wake phrase. All optional: if
        # any of it can't be loaded the call still happens, just less personal.
        self.memory = memory or JarvisMemory()
        self.os_tools = OSTools()
        self._settings = load_settings()
        self._rules = load_rules(self._settings)
        self._end_call_tool = EndCallTool(
            extra_description=(
                "Only end the call after the user clearly says they are finished, "
                "says goodbye, or directly asks to end the call."
            ),
            end_instructions=(
                "Give Jarvis's brief, polite British-English farewell, then end the call."
            ),
        )
        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            # llm=inference.LLM(model="google/gemma-4-31b-it"),
            llm=google.beta.realtime.RealtimeModel(
                model="gemini-3.1-flash-live-preview",
                voice="Enceladus",
                language="en-GB",
                tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
            ),
            # To use a realtime model instead of a voice pipeline, replace the LLM
            # with a RealtimeModel and remove the STT/TTS from the AgentSession
            # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/)
            # 1. Install livekit-agents[openai]
            # 2. Set OPENAI_API_KEY in .env.local
            # 3. Add `from livekit.plugins import openai` to the top of this file
            # 4. Replace the llm argument with:
            #     llm=openai.realtime.RealtimeModel(voice="marin")
            instructions=AGENT_INSTRUCTIONS + "\n\n" + extra_instructions(
                self.memory, self._settings, self._rules),
            tools=[
                *self.browser_tools.tools,
                *self.memory.tools,
                *self.os_tools.tools,
                *self._end_call_tool.tools,
            ],
        )


server = AgentServer()


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # The settings panel in the web page talks to this. It used to run in its
    # own minimised window, which meant that when it failed it closed instantly
    # and the only symptom was "the settings don't work". In here, it fails
    # where you can read it.
    serve_in_background()

    # Headless on purpose. This browser exists so Jarvis can read pages and
    # answer questions; anything meant for you to look at opens in your own
    # Chrome instead. Visible, it was a second window showing a logged-out
    # version of whatever you had just asked for.
    browser = BrowserManager(headless=True)
    ctx.add_shutdown_callback(browser.close)

    # One conversation row per call, so every turn is searchable later.
    memory = JarvisMemory()
    memory.start_conversation("voice")

    # Gemini realtime handles the voice input and output for this session.
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        # stt=inference.STT(model="deepgram/nova-3", language="en"),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        # tts=inference.TTS(
        #   model="fishaudio/s2.1-pro", voice="fa4c9eb3dccc4806b382b40d61c6b10a"
        # ),
        turn_handling=TurnHandlingOptions(
            # The LiveKit turn detector determines when the user is done speaking and the agent should respond.
            # TurnDetector is an end-of-turn model that listens to the user's audio directly, combining
            # semantic understanding with acoustic cues (intonation, pitch, rhythm) for state-of-the-art accuracy.
            # AgentSession supplies the required VAD automatically.
            # See more at https://docs.livekit.io/agents/build/turns
            turn_detection=inference.TurnDetector(),
            # Adaptive interruptions use the turn detector to tell a real interruption from a
            # backchannel like "mhm" or "right", so the agent keeps talking through the latter.
            interruption={"mode": "adaptive"},
            # allow the LLM to generate a response while waiting for the end of turn
            # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
            preemptive_generation={"enabled": True},
        ),
        # Expressive mode injects the TTS provider's markup guide into the LLM prompt, so the model
        # emits inline delivery tags (emotion, pacing, non-verbal sounds) that the TTS renders and
        # the transcript never shows. Requires a TTS model that supports markup, such as the Fish
        # Audio model above.
        # expressive=True,
    )

    _record_conversation(session, memory)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(browser, memory),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            video_input=True,
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )

    # Join the room and connect to the user
    await ctx.connect()


def _record_conversation(session: AgentSession, memory: JarvisMemory) -> None:
    """Write every turn to the shared memory database as it happens.

    Both handlers are wrapped: an exception raised inside a session event
    handler takes the session down with it, and nothing here is worth losing a
    conversation over.
    """

    @session.on("user_input_transcribed")
    def _on_user_speech(event: object) -> None:
        try:
            if not getattr(event, "is_final", True):
                return
            memory.log("user", getattr(event, "transcript", "") or "")
        except Exception:
            pass

    @session.on("conversation_item_added")
    def _on_item(event: object) -> None:
        try:
            item = getattr(event, "item", None)
            if getattr(item, "role", "") == "assistant":
                memory.log("assistant", getattr(item, "text_content", "") or "")
        except Exception:
            pass


if __name__ == "__main__":
    cli.run_app(server)
