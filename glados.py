import copy
import json
import queue
import re
import sys
import threading
import time
from urllib.parse import urljoin

from pathlib import Path
from typing import List
from jinja2 import Template

import numpy as np
import requests
import sounddevice as sd
from Levenshtein import distance
from loguru import logger

from glados import asr, llama, tts, vad, config


logger.remove(0)
logger.add(sys.stderr, level="INFO")


class Glados:
    def __init__(
        self,
        conf: config.Config,
    ) -> None:
        """
        Initializes the VoiceRecognition class, setting up necessary models, streams, and queues.

        This class is not thread-safe, so you should only use it from one thread. It works like this:
        1. The audio stream is continuously listening for input.
        2. The audio is buffered until voice activity is detected. This is to make sure that the
            entire sentence is captured, including before voice activity is detected.
        2. While voice activity is detected, the audio is stored, together with the buffered audio.
        3. When voice activity is not detected after a short time (the PAUSE_LIMIT), the audio is
            transcribed. If voice is detected again during this time, the timer is reset and the
            recording continues.
        4. After the voice stops, the listening stops, and the audio is transcribed.
        5. If a wake word is set, the transcribed text is checked for similarity to the wake word.
        6. The function is called with the transcribed text as the argument.
        7. The audio stream is reset (buffers cleared), and listening continues.

        Args:
            wake_word (str, optional): The wake word to use for activation. Defaults to None.
        """

        self._conf = conf

        self._setup_audio_stream()
        self._setup_vad_model()
        self._setup_asr_model()
        self._setup_tts_model()
        self._setup_llama_model()

        # Initialize sample queues and state flags
        self.samples = []
        self.sample_queue = queue.Queue()
        self.buffer = queue.Queue(maxsize=self._conf.BUFFER_SIZE // self._conf.VAD_SIZE)
        self.recording_started = False
        self.gap_counter = 0
        self.wake_word = self._conf.WAKE_WORD

        self.messages = copy.deepcopy(self._conf.INITIAL_MESSAGES)
        self.llm_queue = queue.Queue()
        self.tts_queue = queue.Queue()
        self.processing = False

        self.shutdown_event = threading.Event()

        self.template = Template(self._conf.LLAMA3_TEMPLATE)

        llm_thread = threading.Thread(target=self.process_LLM)
        llm_thread.start()

        tts_thread = threading.Thread(target=self.process_TTS_thread)
        tts_thread.start()

        audio = self.tts.generate_speech_audio(self._conf.START_ANNOUNCEMENT)
        logger.success(f"TTS text: {self._conf.START_ANNOUNCEMENT}")
        sd.play(audio, tts.RATE)

    def _setup_audio_stream(self):
        """
        Sets up the audio input stream with sounddevice.
        """
        try:
            self.input_stream = sd.InputStream(
                samplerate=self._conf.SAMPLE_RATE,
                channels=1,
                callback=self.audio_callback,
                blocksize=int(self._conf.SAMPLE_RATE * self._conf.VAD_SIZE / 1000),
            )
        except sd.PortAudioError as e:
            logger.error(f"Couldn't open PortAudio device. Your audio may not be properly configured. On Linux, you may need to create an appropriate ~/.asoundrc file. The full error was {e!r}")
            sys.exit(1)

    def _setup_vad_model(self):
        """
        Loads the Voice Activity Detection (VAD) model.
        """
        self.vad_model = vad.VAD(model_path=str(Path.cwd() / "models" / self._conf.VAD_MODEL))

    def _setup_asr_model(self):
        self.asr_model = asr.ASR(model=str(Path.cwd() / "models" / self._conf.ASR_MODEL))

    def _setup_tts_model(self):
        self.tts = tts.TTSEngine(use_cuda=self._conf.TTS_USE_CUDA)

    def _setup_llama_model(self):
        if self._conf.LLAMA_SERVER_EXTERNAL:
            self.llama = llama.ExternalLlamaServer(
                server_base_url=self._conf.LLAMA_SERVER_BASE_URL,
                request_headers=self._conf.LLAMA_SERVER_HEADERS,
            )
        else:
            model_path = Path.cwd() / "models" / self._conf.LLM_MODEL

            self.llama = llama.ChildLlamaServer(
                server_base_url=self._conf.LLAMA_SERVER_BASE_URL,
                request_headers=self._conf.LLAMA_SERVER_HEADERS,
                llama_server_path=self._conf.LLAMA_SERVER_PATH,
                port=self._conf.LLAMA_SERVER_PORT,
                model=None if self._conf.LLAMA_SERVER_EXTERNAL else model_path,
                external=self._conf.LLAMA_SERVER_EXTERNAL,
                use_gpu=True,
            )

        running = self.llama.await_running()

        if not running:
            logger.warning("Llama server does not appear to be running; attempting to continue as it may recover.")

    def audio_callback(self, indata, frames, time, status):
        """
        Callback function for the audio stream, processing incoming data.
        """
        data = indata.copy()
        data = data.squeeze()  # Reduce to single channel if necessary
        vad_confidence = self.vad_model.process_chunk(data) > self._conf.VAD_THRESHOLD
        self.sample_queue.put((data, vad_confidence))

    def start(self):
        """
        Starts the Glados voice assistant, continuously listening for input and responding.
        """
        self.input_stream.start()
        logger.success("Audio Modules Operational")
        self._listen_and_respond()

    def _listen_and_respond(self):
        """
        Listens for audio input and responds appropriately when the wake word is detected.

        This function runs in a loop, listening for audio input and processing it when the wake word is detected.
        It is wrapped in a try-except block to allow for a clean shutdown when a KeyboardInterrupt is detected.
        """
        logger.success("Listening...")
        try:
            while (
                True
            ):  # Loop forever, but is 'paused' when new samples are not available
                sample, vad_confidence = self.sample_queue.get()
                self._handle_audio_sample(sample, vad_confidence)
        except KeyboardInterrupt:
            self.llama.stop()
            self.shutdown_event.set()

    def _handle_audio_sample(self, sample, vad_confidence):
        """
        Handles the processing of each audio sample.

        If the recording has not started, the sample is added to the circular buffer.

        If the recording has started, the sample is added to the samples list, and the pause
        limit is checked to determine when to process the detected audio.

        Args:
            sample (np.ndarray): The audio sample to process.
            vad_confidence (bool): Whether voice activity is detected in the sample.
        """
        if not self.recording_started:
            self._manage_pre_activation_buffer(sample, vad_confidence)
        else:
            self._process_activated_audio(sample, vad_confidence)

    def _manage_pre_activation_buffer(self, sample, vad_confidence):
        """
        Manages the circular buffer of audio samples before activation (i.e., before the voice is detected).

        If the buffer is full, the oldest sample is discarded to make room for new ones.

        If voice activity is detected, the audio stream is stopped, and the processing is turned off
        to prevent overlap with the LLM and TTS threads.

        Args:
            sample (np.ndarray): The audio sample to process.
            vad_confidence (bool): Whether voice activity is detected in the sample.
        """
        if self.buffer.full():
            self.buffer.get()  # Discard the oldest sample to make room for new ones
        self.buffer.put(sample)

        if vad_confidence:  # Voice activity detected
            sd.stop()  # Stop the audio stream to prevent overlap
            self.processing = (
                False  # Turns off processing on threads for the LLM and TTS!!!
            )
            self.samples = list(self.buffer.queue)
            self.recording_started = True

    def _process_activated_audio(self, sample: np.ndarray, vad_confidence: bool):
        """
        Processes audio samples after activation (i.e., after the wake word is detected).

        Uses a pause limit to determine when to process the detected audio. This is to
        ensure that the entire sentence is captured before processing, including slight gaps.
        """

        self.samples.append(sample)

        if not vad_confidence:
            self.gap_counter += 1
            if self.gap_counter >= self._conf.PAUSE_LIMIT // self._conf.VAD_SIZE:
                self._process_detected_audio()
        else:
            self.gap_counter = 0

    def _wakeword_detected(self, text: str) -> bool:
        """
        Calculates the nearest Levenshtein distance from the detected text to the wake word.

        This is used as 'Glados' is not a common word, and Whisper can sometimes mishear it.
        """
        words = text.split()
        closest_distance = min(
            [distance(word.lower(), self.wake_word) for word in words]
        )
        return closest_distance < self._conf.SIMILARITY_THRESHOLD

    def _process_detected_audio(self):
        """
        Processes the detected audio and generates a response.

        This function is called when the pause limit is reached after the voice stops.
        It transcribes the audio and checks for the wake word if it is set. If the wake
        word is detected, the detected text is sent to the LLM model for processing.
        The audio stream is then reset, and listening continues.
        """
        logger.debug("Detected pause after speech. Processing...")
        self.input_stream.stop()

        detected_text = self.asr(self.samples)
        hallucination = detected_text and any(hallucination.lower() == detected_text.lower() for hallucination in self._conf.STT_HALLUCINATIONS)

        if detected_text and not hallucination:
            logger.success(f"ASR text: '{detected_text}'")

            if self.wake_word is not None:
                if self._wakeword_detected(detected_text):
                    logger.info("Wake word detected!")

                    self.llm_queue.put(detected_text)
                    self.processing = True
                else:
                    logger.info("No wake word detected. Ignoring...")
            else:
                self.llm_queue.put(detected_text)
                self.processing = True

        elif hallucination:
            logger.success(f"ASR text: '{detected_text}' (NOTE: ignored, as a probable hallucination from the TTS model)")
            self.processing = True
        else:
            logger.info("Heard audio, but didn't detect any speech within it.")
            self.processing = True

        self.reset()
        self.input_stream.start()

    def asr(self, samples: List[np.ndarray]) -> str:
        """
        Performs automatic speech recognition on the collected samples.
        """
        audio = np.concatenate(samples)

        detected_text = self.asr_model.transcribe(audio)
        return detected_text

    def reset(self):
        """
        Resets the recording state and clears buffers.
        """
        logger.debug("Resetting recorder...")
        self.recording_started = False
        self.samples.clear()
        self.gap_counter = 0
        with self.buffer.mutex:
            self.buffer.queue.clear()

    def process_TTS_thread(self):
        """
        Processes the LLM generated text using the TTS model.

        Runs in a separate thread to allow for continuous processing of the LLM output.
        """
        assistant_text = (
            []
        )  # The text generated by the assistant, to be spoken by the TTS
        system_text = (
            []
        )  # The text logged to the system prompt when the TTS is interrupted
        finished = False  # a flag to indicate when the TTS has finished speaking
        interrupted = (
            False  # a flag to indicate when the TTS was interrupted by new input
        )

        while not self.shutdown_event.is_set():
            try:
                generated_text = self.tts_queue.get(timeout=self._conf.PAUSE_TIME)

                if generated_text == "<EOS>":  # End of stream token generated in process_LLM_thread
                    finished = True
                elif not generated_text:
                    logger.warning("Empty string sent to TTS")  # should not happen!
                else:
                    logger.success(f"TTS text: {generated_text}")
                    audio = self.tts.generate_speech_audio(generated_text)
                    total_samples = len(audio)

                    if total_samples:
                        sd.play(audio, tts.RATE)

                        interrupted, percentage_played = self.percentage_played(
                            total_samples
                        )

                        if interrupted:
                            clipped_text = self.clip_interrupted_sentence(
                                generated_text, percentage_played
                            )

                            logger.info(
                                f"TTS interrupted at {percentage_played}%: {clipped_text}"
                            )
                            system_text = copy.deepcopy(assistant_text)
                            system_text.append(clipped_text)
                            finished = True

                        assistant_text.append(generated_text)

                if finished:
                    if isinstance(assistant_text, list):
                        logger.warning("assistant_text is a list, somehow. Seems to relate to EOS tokens. Working around it.")
                        assistant_text = assistant_text[0]

                    self.messages.append(
                        {"role": "assistant", "content": " ".join(assistant_text)}
                    )
                    # if interrupted:
                    #     self.messages.append(
                    #         {
                    #             "role": "system",
                    #             "content": f"USER INTERRUPTED GLADOS, TEXT DELIVERED: {' '.join(system_text)}",
                    #         }
                    #     )
                    assistant_text = []
                    finished = False
                    interrupted = False

            except queue.Empty:
                pass

    def clip_interrupted_sentence(self, generated_text, percentage_played):
        """
        Clips the generated text if the TTS was interrupted.

        Args:

            generated_text (str): The generated text from the LLM model.
            percentage_played (float): The percentage of the audio played before the TTS was interrupted.

            Returns:

            str: The clipped text.

        """
        tokens = generated_text.split()
        words_to_print = round((percentage_played / 100) * len(tokens))
        text = " ".join(tokens[:words_to_print])

        # If the TTS was cut off, make that clear
        if words_to_print < len(tokens):
            text = text + "<INTERRUPTED>"
        return text

    def percentage_played(self, total_samples):
        interrupted = False
        start_time = time.time()
        played_samples = 0

        try:
            while sd.get_stream().active:
                time.sleep(self._conf.PAUSE_TIME)  # Should the TTS stream should still be active?
                if self.processing is False:
                    sd.stop()  # Stop the audio stream
                    self.tts_queue = queue.Queue()  # Clear the TTS queue
                    interrupted = True
                    break
        except sd.PortAudioError as e:
            logger.warning(f"PortAudioError during playback: {e!r}. Ignoring.")

        elapsed_time = (
            time.time() - start_time + 0.12
        )  # slight delay to ensure all audio timing is correct
        played_samples = elapsed_time * tts.RATE

        # Calculate percentage of audio played
        percentage_played = min(int((played_samples / total_samples * 100)), 100)
        return interrupted, percentage_played

    def process_LLM(self):
        """
        Processes the detected text using the LLM model.

        """
        while not self.shutdown_event.is_set():
            try:
                detected_text = self.llm_queue.get(timeout=0.1)

                self.messages.append({"role": "user", "content": detected_text})

                prompt = self.template.render(
                    messages=self.messages,
                    bos_token="<|begin_of_text|>",
                    add_generation_prompt=True,
                )

                data = {
                    "stream": True,
                    "prompt": prompt,
                    # "stop": ["\n", "<|im_end|>"],
                    # "messages": self.messages,
                }
                logger.debug(f"starting request on {self.messages=}")
                logger.debug("Perfoming request to LLM server...")

                # Perform the request and process the stream
                try:
                    with self.llama.request(
                        json=data,
                        stream=True,
                    ) as response:
                        if not response.ok:
                            logger.error(f"Couldn't obtain a response from the LLM this time; ignoring.")
                            continue

                        else:
                            logger.info(f"Got successful response from AI: {response.text!r}")

                        sentence = []
                        for line in response.iter_lines():
                            if self.processing is False:
                                break  # If the stop flag is set from new voice input, halt processing
                            if line:  # Filter out empty keep-alive new lines
                                line = self._clean_raw_bytes(line)
                                next_token = self._process_line(line)
                                if next_token:
                                    sentence.append(next_token)
                                    # If there is a pause token, send the sentence to the TTS queue
                                    if next_token in [".", "!", "?", ":", ";", "?!"]:
                                        self._process_sentence(sentence)
                                        sentence = []

                        if self.processing and sentence:
                            self.tts_queue.put(sentence)

                        self.tts_queue.put("<EOS>")  # Add end of stream token to the queue

                except requests.exceptions.ConnectionError as e:
                    logger.error("Couldn't connect to AI endpoint at this time. Is it still loading?")

            except queue.Empty:
                time.sleep(self._conf.PAUSE_TIME)

    def _process_sentence(self, current_sentence):
        """
        Join text, remove inflections and actions, and send to the TTS queue.

        The LLM like to *whisper* things or (scream) things, and prompting is not a 100% fix.
        We use regular expressions to remove text between ** and () to clean up the text.
        Finally, we remove any non-alphanumeric characters/punctuation and send the text
        to the TTS queue.
        """
        sentence = "".join(current_sentence)

        for stopword in self._conf.LLM_STOPWORDS:
            sentence = sentence.removesuffix(stopword)

        sentence = re.sub(r"\*.*?\*|\(.*?\)", "", sentence)
        sentence = re.sub(r"[^a-zA-Z0-9.,?!;:'\" -]", "", sentence)
        sentence = sentence + " "  # Add a space to the end of the sentence, for better TTS

        if sentence:
            if sentence in self._conf.AI_OUTPUT_TO_IGNORE:
                logger.warn(f"Ignoring weird AI output: {sentence!r}")

            else:
                self.tts_queue.put(sentence)

    def _process_line(self, line):
        """
        Processes a single line of text from the LLM server.

        Args:
            line (dict): The line of text from the LLM server.
        """

        if not line["stop"]:
            token = line["content"]
            return token
        return None

    def _clean_raw_bytes(self, line):
        """
        Cleans the raw bytes from the LLM server for processing.

        Coverts the bytes to a dictionary.

        Args:
            line (bytes): The raw bytes from the LLM server.
        """
        line = line.decode("utf-8")
        line = line.removeprefix("data: ")
        line = json.loads(line)
        return line


def load_config() -> config.Config:
    try:
        import user_config
        cfg = user_config.config
        logger.info("Loaded config from user_config.py")

    except ImportError:
        logger.warning("No ./user_config.py file found (or could not load it!). Using defaults (which probably won't work)!")
        cfg = config.Config()

    return cfg


def main():
    cfg = load_config()

    demo = Glados(conf=cfg)
    demo.start()

    return 0


if __name__ == "__main__":
    sys.exit(main())
