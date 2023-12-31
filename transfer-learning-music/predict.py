# Prediction interface for Cog ⚙️
# Reference: https://github.com/replicate/cog/blob/main/docs/python.md

from pathlib import Path
import tempfile

from cog import BasePredictor, Input, Path
import youtube_dl
from essentia.streaming import (
    MonoLoader,
    FrameCutter,
    VectorRealToTensor,
    TensorToPool,
    TensorflowInputMusiCNN,
    TensorflowInputVGGish,
    TensorflowPredict,
    PoolToTensor,
    TensorToVectorReal,
)
from essentia import Pool, run
import numpy as np

from models import models

MODELS_HOME = "/models"


class Predictor(BasePredictor):
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""
        self.sample_rate = 16000

    def predict(
        self,
        audio: Path = Input(
            description="Audio file to process",
            default=None,
        ),
        url: str = Input(
            description="YouTube URL to process (overrides audio input)",
            default="",
        ),
        model_type: str = Input(
            description="Model type (embeddings)",
            default="musicnn-msd-2",
            choices=["musicnn-msd-2", "musicnn-mtt-2", "vggish-audioset-1"],
        )
    ) -> Path:
        """Run a single prediction by all models of the selected type"""

        assert audio or url, "Specify either an audio filename or a YouTube url"

        # If there is a YouTube url use that.
        if url:
            if audio:
                print(
                    "Warning: Both `url` and `audio` inputs were specified. "
                    "The `url` will be process. To process the `audio` input clear the `url` input field."
                )
            audio, title = self._download(url)
        else:
            title = audio.name

        # Configure a processing chain based on the selected model type.
        pool = Pool()
        loader = MonoLoader(filename=str(audio), sampleRate=self.sample_rate)

        patch_hop_size = 0  # No overlap for efficiency
        batch_size = 256

        if model_type in ["musicnn-msd-2", "musicnn-mtt-2"]:
            frame_size = 512
            hop_size = 256
            patch_size = 187
            nbands = 96
            melSpectrogram = TensorflowInputMusiCNN()
        elif model_type in ["vggish-audioset-1"]:
            frame_size = 400
            hop_size = 160
            patch_size = 96
            nbands = 64
            melSpectrogram = TensorflowInputVGGish()

        frameCutter = FrameCutter(
            frameSize=frame_size,
            hopSize=hop_size,
            silentFrames="keep",
        )
        vectorRealToTensor = VectorRealToTensor(
            shape=[batch_size, 1, patch_size, nbands],
            patchHopSize=patch_hop_size,
        )
        tensorToPool = TensorToPool(namespace="model/Placeholder")

        # Algorithms for specific models.
        tensorflowPredict = {}
        poolToTensor = {}
        tensorToVectorReal = {}

        for model in models:
            modelFilename = "/models/%s-%s.pb" % (model["name"], model_type)
            tensorflowPredict[model["name"]] = TensorflowPredict(
                graphFilename=modelFilename,
                inputs=["model/Placeholder"],
                outputs=["model/Sigmoid"],
            )
            poolToTensor[model["name"]] = PoolToTensor(namespace="model/Sigmoid")
            tensorToVectorReal[model["name"]] = TensorToVectorReal()

        loader.audio >> frameCutter.signal
        frameCutter.frame >> melSpectrogram.frame
        melSpectrogram.bands >> vectorRealToTensor.frame
        vectorRealToTensor.tensor >> tensorToPool.tensor

        for model in [model["name"] for model in models]:
            tensorToPool.pool >> tensorflowPredict[model].poolIn
            tensorflowPredict[model].poolOut >> poolToTensor[model].pool
            (poolToTensor[model].tensor >> tensorToVectorReal[model].tensor)
            tensorToVectorReal[model].frame >> (pool, "activations.%s" % model)

        print("running the inference network...")
        run(loader)

        title = "# %s\n" % title
        header = "| model | class | activation |\n"
        bar = "|---|---|---|\n"
        table = title + header + bar
        for model in models:
            average = np.mean(pool["activations.%s" % model["name"]], axis=0)

            labels = []
            activations = []

            top_class = np.argmax(average)
            for i, label in enumerate(model["labels"]):
                labels.append(label)
                if i == top_class:
                    activations.append(f"**{average[i]:.2f}**")
                else:
                    activations.append(f"{average[i]:.2f}")

            labels = "<br>".join(labels)
            activations = "<br>".join(activations)

            table += f"{model['name']} | {labels} | {activations}\n"
            if model != models[-1]:
                table += "||<hr>|<hr>|\n"  # separator for readability

        out_path = Path(tempfile.mkdtemp()) / "out.md"
        with open(out_path, "w") as f:
            f.write(table)
        return out_path

    def _download(self, url, ext="wav"):
        """Download a YouTube URL in the specified format to a temporal directory"""

        tmp_dir = Path(tempfile.mktemp())
        ydl_opts = {
            # The download is quite slow, use the most compressed format that doesn't affect
            # the sense of the prediction (too much):
            #
            # Code  Container  Audio Codec  Audio Bitrate     Channels    Still offered?
            # 250   WebM       Opus (VBR)   ~70 Kbps          Stereo (2)  Yes
            # 251   WebM       Opus         (VBR) <=160 Kbps  Stereo (2)  Yes
            # 40    MP4        AAC (LC)     128 Kbps          Stereo (2)  Yes, YT Music
            #
            # Download speeds:
            # 250 -> ~19s, 251 -> 30s, 40 -> ~35s
            # but 250 changes the predictions too munch. Using 251 as a compromise.
            #
            # From https://gist.github.com/AgentOak/34d47c65b1d28829bb17c24c04a0096f
            "format": "251",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": ext,
                }
            ],
            # render audio @16kHz to prevent resampling latter on
            "postprocessor_args": ["-ar", f"{self.sample_rate}"],
            "outtmpl": str(tmp_dir / "audio.%(ext)s"),
        }

        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)

            if "title" in info:
                title = info["title"]
            else:
                title = ""  # is it possible that the title metadata is unavailable? Continue anyway

        paths = [p for p in tmp_dir.glob(f"audio.{ext}")]
        assert (
            len(paths) == 1
        ), "Something unexpected happened. Should be only one match!"

        return paths[0], title
