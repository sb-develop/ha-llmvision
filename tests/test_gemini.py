import os
import shutil
import logging
import base64
import subprocess
import io
import json
from typing import Optional, List, Tuple
import numpy as np
from PIL import Image
from google import genai
from google.genai import types

_LOGGER = logging.getLogger(__name__)


# copied from media_handlers.py
def _similarity_score(previous_frame: str, current_frame_gray: str):
    """
    SSIM by Z. Wang: https://ece.uwaterloo.ca/~z70wang/research/ssim/
    Paper:  Z. Wang, A. C. Bovik, H. R. Sheikh and E. P. Simoncelli,
    "Image quality assessment: From error visibility to structural similarity," IEEE Transactions on Image Processing, vol. 13, no. 4, pp. 600-612, Apr. 2004.
    """
    K1 = 0.005
    K2 = 0.015
    L = 255

    C1 = (K1 * L) ** 2
    C2 = (K2 * L) ** 2

    # Decode base64 strings into images
    previous_frame = Image.open(io.BytesIO(base64.b64decode(previous_frame)))
    current_frame_gray = Image.open(io.BytesIO(base64.b64decode(current_frame_gray)))

    # Convert images to grayscale
    previous_frame = previous_frame.convert("L")
    current_frame_gray = current_frame_gray.convert("L")

    previous_frame_np = np.array(previous_frame)
    current_frame_np = np.array(current_frame_gray)

    # Ensure both frames have same dimensions
    if previous_frame_np.shape != current_frame_np.shape:
        min_shape = np.minimum(
            previous_frame_np.shape, current_frame_np.shape)
        previous_frame_np = previous_frame_np[:min_shape[0], :min_shape[1]]
        current_frame_np = current_frame_np[:min_shape[0], :min_shape[1]]

    # Calculate mean (mu)
    mu1 = np.mean(previous_frame_np, dtype=np.float64)
    mu2 = np.mean(current_frame_np, dtype=np.float64)

    # Calculate variance (sigma^2) and covariance (sigma12)
    sigma1_sq = np.var(previous_frame_np, dtype=np.float64)
    sigma2_sq = np.var(current_frame_np, dtype=np.float64)
    sigma12 = np.cov(previous_frame_np.flatten(),
                        current_frame_np.flatten(),
                        dtype=np.float64)[0, 1]

    # Calculate SSIM
    ssim = ((2 * mu1 * mu2 + C1) * (2 * sigma12 + C2)) / \
        ((mu1**2 + mu2**2 + C1) * (sigma1_sq + sigma2_sq + C2))

    return ssim


class TestWorkflow:
    def __init__(self,
                 video_path: str,
                 ffmpeg_directory: str,
                 sys_instruction: str,
                 prompt: str,
                 memory_prompt: str,
                 memories: Optional[List[Tuple[str, str]]],
                 gemini_api_key: str,
                 gemini_model: str = "gemini-2.0-flash"):
        self.video_path = video_path
        self.ffmpeg_directory = ffmpeg_directory
        self.gemini_api_key = gemini_api_key
        self.gemini_model = gemini_model
        self.sys_instruction = sys_instruction
        self.prompt = prompt
        self.memory_prompt = memory_prompt
        self.memories = memories
        self.tmp_frames_dir = os.path.join(os.path.dirname(__file__), "tmp_frames")
        self.results = []

    def extract_frames(self,
                       max_frames: int = 5,
                       target_width: int = 512) -> List[Tuple[str, Optional[float]]]:
        """Extract frames from the video file and calculate similarity scores."""
        os.makedirs(self.tmp_frames_dir, exist_ok=True)
        ffmpeg_cmd = [
            os.path.join(self.ffmpeg_directory, "ffmpeg"),
            "-hide_banner",
            "-hwaccel", "auto",
            "-skip_frame", "nokey",
            "-an", "-sn", "-dn",
            "-i", self.video_path,
            "-fps_mode", "passthrough",
            os.path.join(self.tmp_frames_dir, "frame%05d.jpg")
        ]
        subprocess.run(' '.join(ffmpeg_cmd), check=True)

        frames = []
        for frame_file in sorted(os.listdir(self.tmp_frames_dir)):
            frame_path = os.path.join(self.tmp_frames_dir, frame_file)
            resized_image = self.resize_image(target_width=target_width, image_path=frame_path)
            frames.append((resized_image, 0))
            if len(frames) >= 2:
                frames[-1] = (frames[-1][0], _similarity_score(frames[-2][0], frames[-1][0]))
        if len(frames) > max_frames:
            frames = frames[:max_frames]
        return frames

    def resize_image(self,
                     target_width: Optional[int],
                     image_path: str) -> str:
        """Resize image to the target width and return it as a base64 string."""
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            if target_width is not None:
                width, height = img.size
                aspect_ratio = width / height
                target_height = int(target_width / aspect_ratio)

                if width > target_width or height > target_height:
                    img = img.resize((target_width, target_height))

            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format="JPEG")
        return base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")

    def upload_to_gemini(self,
                         frames: List[Tuple[str, Optional[float]]],
                         memories: Optional[List[Tuple[str, str]]],
                         json_response: bool = True) -> dict:
        """Upload frames and memory to Gemini for analysis."""
        client = genai.Client(api_key=self.gemini_api_key)

        contents = [types.Part.from_text(text=self.prompt)]
        for i, (image, score) in enumerate(frames):
            contents.append(types.Part.from_text(text=f"Video frame {i + 1}:"))
            contents.append(types.Part.from_bytes(data=base64.b64decode(image), mime_type='image/jpeg'))
        if memories and len(memories) > 0 and self.memory_prompt:
            contents.append(types.Part.from_text(text=self.memory_prompt))
            for description, image in memories:
                contents.append(types.Part.from_text(text=description))
                contents.append(types.Part.from_bytes(data=base64.b64decode(image), mime_type='image/jpeg'))

        # Collect response text from the Gemini API
        response_text = client.models.generate_content(
            model=self.gemini_model,
            config=types.GenerateContentConfig(
                system_instruction=self.sys_instruction,
                max_output_tokens=150,
                temperature=0.2,
            ),
            contents=contents,
        ).text

        return self._parse_response_text(response_text, json_response)

    def _parse_response_text(self, response_text: str, json_response: bool) -> dict:
        """Parse the response text into a dictionary."""
        if json_response:
            try:
                json_text = response_text.strip("```").lstrip("json")
                return json.loads(json_text)
            except json.JSONDecodeError:
                pass
        return {"response_text": response_text}

    def run_workflow(self, max_frames: int = 5, target_width: int = 512) -> dict:
        """Run the complete workflow: extract frames, upload to Gemini, and collect results."""
        try:
            _LOGGER.info("Extracting frames from video...")
            frames = self.extract_frames(max_frames=max_frames, target_width=target_width)
            
            _LOGGER.info("Preparing memory images and descriptions...")
            memories = [(description, self.resize_image(target_width=target_width, image_path=image_path)) for description, image_path in self.memories]

            _LOGGER.info("Uploading frames to Gemini for analysis...")
            response = self.upload_to_gemini(frames, memories)

            _LOGGER.info("Collecting results...")
            self.results = response
            return self.results
        finally:
            _LOGGER.info("Cleaning up temporary files...")
            shutil.rmtree(self.tmp_frames_dir, ignore_errors=True)

# Constants
SYS_INSTRUCTION = '''You are an expert AI analyzing a series of time-sequenced images from a motion-activated security camera. Your primary goal is to provide a highly detailed and accurate description of the scene and identify any individuals or objects that match the provided "Known Entities".

**Instructions:**
1.  **Scene Analysis:** Analyze the security camera images, noting any changes or progression of events. Provide a description of the scene without being overly verbose and include details such as:
    * **Objects Present:** Identify and describe visible dynamic objects (such as persons, animals or vehicles), including their approximate location within the frame. Ignore static objects and scenery.
    * **Actions and Interactions:** Describe any actions being performed by individuals or movements of objects. Note any interactions between entities.
    * **Focus on Factual Observation:** Base your description and identifications solely on the visual information present in the provided images. Avoid making assumptions or inferences beyond what is directly observable.
2.  **Precise Entity Matching:** When comparing entities in the images to the "Known Entities", focus on specific distinguishing features.
    * **Explicit Identification:** If a match is found, explicitly state the "Name" of the known entity in your description.
    * **Handling of Uncertainty:** If an entity resembles a known entity but the match is not definitive, state that a potential match was observed but could not be confirmed. Do not make speculative identifications.
3.  **If no movement is detected, respond with: "No activity observed."

**Structured Output Format:**
Provide your response as a raw JSON object, without any surrounding formatting like the markdown backticks ``` or the json identifier itself.

{
    "title": string,
    "response_text": string,
    "known_entities": list[string],
}

1. "title" is a short and concise summary of the event. The title should summarize the key actions or events captured in the images, be suitable for use in a notification or alert and be shorter than 6 words. The title should be in the format: "<Object> <action>", e.g., "Person walking", "Car passing", "Animal detected".
2. "response_text" is a detailed description of the scene, including the actions of the entities and their interactions. It should be factual and based solely on the visual information present in the images.
3. "known_entities" is a list of all detected know entities, including their names, that were detected in the images. If no known entities are detected, return an empty list.


**Example:**
Output for an image showing a delivery person:

{
    "title": "Person detected at door",
    "response_text": "An unidentified adult male carrying a package is walking towards the front door.",
    "known_entities": [],
}
'''

PROMPT = '''Analyze the series of images captured at short intervals by a motion-activated security camera. Give your single-line answer in German.'''

MEMORY_PROMPT = '''The following images are "known entities" along with descriptions.'''

MEMORIES = [
    ("This is A", r"<PATH TO IMAGE>"),
    ("This is B", r"<PATH TO IMAGE>"),
    ("This is C", r"<PATH TO IMAGE>"),
    ("This is D and her dog E", r"<PATH TO IMAGE>"),
]

# Example usage
if __name__ == "__main__":
    workflow = TestWorkflow(video_path=r"<PATH TO VIDEO FILE>",
                            ffmpeg_directory=r"<PATH TO FFMPEG>",
                            sys_instruction=SYS_INSTRUCTION,
                            prompt=PROMPT,
                            memory_prompt=MEMORY_PROMPT,
                            memories=MEMORIES,
                            gemini_api_key="<API KEY>")
    results = workflow.run_workflow(max_frames=3, target_width=512)
    print(results)
