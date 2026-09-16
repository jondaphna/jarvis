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
from files import FileTools
from learning import Lessons
from os_tools import OSTools
from permissions import filter_tools
from permissions import summary as permission_summary
from personalise import assemble, fingerprint, load_rules, load_settings
from prompts import AGENT_INSTRUCTIONS
from thinker import Thinker
from tools import BrowserTools

load_dotenv(".env.local")


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None,
                 memory: JarvisMemory | None = None,
                 lessons: Lessons | None = None) -> None:
        self.browser = browser or BrowserManager(headless=True)
        self.browser_tools = BrowserTools(self.browser)
        # Memory, your custom commands and the wake phrase. All optional: if
        # any of it can't be loaded the call still happens, just less personal.
        self.memory = memory or JarvisMemory()
        # What you have taught it. Shares the memory handle rather than
        # opening a second connection to the same SQLite file.
        self.lessons = lessons or Lessons(self.memory)
        self.os_tools = OSTools()
        # Your own documents, searched locally and read back. Free, and
        # nothing about them leaves the machine except the part it quotes.
        self.file_tools = FileTools()
        # The smart half. The voice stays fast; this is where hard
        # problems go.
        self.thinker = Thinker()
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
        self._prompt_fingerprint = fingerprint(self._settings, self._rules)
        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            # llm=inference.LLM(model="google/gemma-4-31b-it"),
            llm=google.beta.realtime.RealtimeModel(
                model="gemini-3.1-flash-live-preview",
                voice="Enceladus",
                language="en-GB",
                tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
                # Why the conversation used to die after a while.
                #
                # A Gemini Live session has a time limit. When it is nearly up
                # the server sends "go away", and the plugin's handler for that
                # simply closes the session - its own comment says the
                # reconnection "isn't seamless just yet". So Jarvis went quiet
                # mid-conversation and stopped being able to do anything, with
                # nothing on screen to say why.
                #
                # Asking for session resumption is what fixes it. The server
                # then issues a handle, the plugin stores it, and on reconnect
                # it hands the handle back and carries on where it left off.
                # Without this the handle is never issued, so there is nothing
                # to resume with and the session is simply gone.
                session_resumption=genai_types.SessionResumptionConfig(),
                # And the other way a long conversation ends: filling the
                # context window. A sliding window keeps the recent turns and
                # drops the stalest instead of hitting the ceiling.
                context_window_compression=genai_types.ContextWindowCompressionConfig(
                    sliding_window=genai_types.SlidingWindow(),
                ),
            ),
            # To use a realtime model instead of a voice pipeline, replace the LLM
            # with a RealtimeModel and remove the STT/TTS from the AgentSession
            # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/)
            # 1. Install livekit-agents[openai]
            # 2. Set OPENAI_API_KEY in .env.local
            # 3. Add `from livekit.plugins import openai` to the top of this file
            # 4. Replace the llm argument with:
            #     llm=openai.realtime.RealtimeModel(voice="marin")
            # Their own instructions first, the template second, and a
            # reminder of theirs last. Order is not decoration here: the
            # commands written in the settings panel were being ignored, and
            # one reason was that they arrived buried in the middle of two
            # thousand tokens of somebody else's personality.
            instructions=self._instructions(),
            # Anything switched off is removed here rather than refused later.
            # A tool the model was never given is one it cannot try, announce
            # it is trying, or be talked into.
            tools=filter_tools([
                *self.browser_tools.tools,
                *self.memory.tools,
                *self.lessons.tools,
                *self.os_tools.tools,
                *self.file_tools.tools,
                *self.thinker.tools,
                *self._end_call_tool.tools,
            ], self._settings),
        )

    def _instructions(self) -> str:
        """The whole system prompt, rebuilt from what is on disk right now."""
        return assemble(
            AGENT_INSTRUCTIONS, self.memory, self._settings, self._rules,
            extra=(
                # Their own lessons, ahead of the permission notes: this is
                # the block that makes a repeated job land first time.
                self.lessons.block(),
                self.lessons.guidance(),
                permission_summary(self._settings),
            ))

    async def reload_rules(self) -> bool:
        """Pick up an edit to the settings panel without restarting.

        Why this exists: "I write the commands and he just ignores them" is
        indistinguishable, from the outside, from "the running agent has never
        seen them". The instructions are fixed when a call starts, so anything
        typed during a call used to do nothing until the next one - which is
        exactly what being ignored looks like.

        Returns True when something actually changed. Nothing here may raise:
        a settings file half-written by the panel must not end a conversation.
        """
        try:
            settings = load_settings()
            rules = load_rules(settings)
            current = fingerprint(settings, rules)
        except Exception:
            return False
        if current == self._prompt_fingerprint:
            return False
        self._settings, self._rules = settings, rules
        self._prompt_fingerprint = current
        try:
            await self.update_instructions(self._instructions())
        except Exception:
            return False
        print("  (picked up your new instructions)")
        return True


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

    # One window, visible, and the same one every time. Jarvis opens a normal
    # Chrome and then attaches to it, so what you see and what it can act on
    # are the same page - and asking for a site then asking for something on
    # it happens in one tab rather than two.
    browser = BrowserManager()
    ctx.add_shutdown_callback(browser.close)

    # One conversation row per call, so every turn is searchable later.
    memory = JarvisMemory()
    memory.start_conversation("voice")

    # Built here rather than inside the Assistant so the session handlers
    # below and the agent's own tool share one object - the watcher needs to
    # see what the tool just learned, or it overwrites it.
    lessons = Lessons(memory)

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

    _record_conversation(session, memory, lessons)

    assistant = Assistant(browser, memory, lessons)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=assistant,
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

    # Watch for edits to the settings panel while the call is running.
    _watch_for_edits(assistant)

    # Join the room and connect to the user
    await ctx.connect()


#: How often to look for a change to your instructions. Cheap - it hashes what
#: the model would be told and compares, so an idle check does no work at all.
RELOAD_SECONDS = 4.0


def _watch_for_edits(assistant: Assistant) -> None:
    """Keep the running call in step with the settings panel.

    Without this, typing a command during a conversation does nothing until the
    next one, which from the outside is identical to the command being ignored.
    The task is cancelled with the session and can never raise into it.
    """
    import asyncio

    async def loop() -> None:
        while True:
            await asyncio.sleep(RELOAD_SECONDS)
            try:
                await assistant.reload_rules()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    task = asyncio.ensure_future(loop())
    # Held so the loop isn't garbage-collected mid-flight; asyncio only keeps
    # a weak reference to a running task.
    assistant._reload_task = task


def _record_conversation(session: AgentSession, memory: JarvisMemory,
                         lessons: Lessons | None = None) -> None:
    """Write every turn to the shared memory database as it happens.

    Every handler is wrapped: an exception raised inside a session event
    handler takes the session down with it, and nothing here is worth losing a
    conversation over.
    """

    @session.on("user_input_transcribed")
    def _on_user_speech(event: object) -> None:
        try:
            if not getattr(event, "is_final", True):
                return
            said = getattr(event, "transcript", "") or ""
            memory.log("user", said)
            if lessons is not None:
                lessons.heard(said)
        except Exception:
            pass

    @session.on("function_tools_executed")
    def _on_tools_done(event: object) -> None:
        """Learn the recipe that just worked, without being asked to.

        This is the half of learning that costs the user nothing. A request
        that succeeds first time is a demonstration, and writing down what was
        done means the same words next week go straight to the same steps.
        """
        if lessons is None:
            return
        try:
            calls = []
            for call, output in event.zipped():
                calls.append((
                    getattr(call, "name", "") or "",
                    getattr(call, "arguments", "") or "",
                    bool(getattr(output, "is_error", False)) if output else True,
                ))
            lessons.watched(calls)
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
