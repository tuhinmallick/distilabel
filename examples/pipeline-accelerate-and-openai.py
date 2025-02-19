# Copyright 2023-present, Argilla, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# WARNING: To run this example, you will need to install `accelerate` as
# `pip install accelerate`

# Usage: `accelerate launch examples/pipeline-accelerate-and-openai.py`

import os

import torch
from accelerate import Accelerator
from accelerate.utils import gather_object
from datasets import Dataset, load_dataset
from distilabel.dataset import CustomDataset
from distilabel.llm import OpenAILLM, TransformersLLM
from distilabel.pipeline import Pipeline
from distilabel.tasks import TextGenerationTask, UltraFeedbackTask
from transformers import AutoModelForCausalLM, AutoTokenizer


def get_current_device() -> int:
    """Get the current device. For GPU we return the local process index to enable multiple GPU training."""
    return Accelerator().local_process_index if torch.cuda.is_available() else "cpu"


if __name__ == "__main__":
    accelerator = Accelerator()
    with accelerator.local_main_process_first():
        dataset = (
            load_dataset("HuggingFaceH4/instruction-dataset", split="test[:10]")
            .remove_columns(["completion", "meta"])
            .rename_column("prompt", "input")
        )

    model = AutoModelForCausalLM.from_pretrained(
        "HuggingFaceH4/zephyr-7b-beta",
        torch_dtype=torch.bfloat16,
        device_map={"": get_current_device()},
    )
    tokenizer = AutoTokenizer.from_pretrained("HuggingFaceH4/zephyr-7b-beta")
    tokenizer.padding_side = "left"

    pipeline = Pipeline(
        generator=TransformersLLM(
            model=model,
            tokenizer=tokenizer,
            task=TextGenerationTask(),
            max_new_tokens=128,
            temperature=0.3,
            prompt_format="zephyr",
            do_sample=True,
        ),
        labeller=OpenAILLM(
            model="gpt-3.5-turbo",
            task=UltraFeedbackTask.for_instruction_following(),
            max_new_tokens=128,
            num_threads=2,
            openai_api_key=os.getenv("OPENAI_API_KEY", None),
            temperature=0.0,
        ),
    )
    with accelerator.split_between_processes(dataset.to_dict()) as inputs:  # type: ignore
        inputs = Dataset.from_dict(inputs)  # type: ignore
        dataset = pipeline.generate(
            inputs,  # type: ignore
            num_generations=2,
            batch_size=1,
            enable_checkpoints=True,
            display_progress_bar=True,
        )
        dataset = gather_object(dataset)

    # Push to the HuggingFace Hub
    if accelerator.is_main_process:
        dataset = Dataset.from_list(dataset)
        dataset.push_to_hub(
            os.getenv("HF_REPO_ID"),  # type: ignore
            split="train",
            private=True,
            token=os.getenv("HF_TOKEN", None),
        )

        try:
            from uuid import uuid4

            import argilla as rg

            rg.init(
                api_url=os.getenv("ARGILLA_API_URL"),
                api_key=os.getenv("ARGILLA_API_KEY"),
            )

            # Convert into an Argilla dataset and push it to Argilla
            dataset.__class__ = CustomDataset
            dataset.task = UltraFeedbackTask.for_instruction_following()  # type: ignore
            rg_dataset = dataset.to_argilla()  # type: ignore
            rg_dataset.push_to_argilla(
                name=f"my-dataset-{uuid4()}",
                workspace="admin",
            )
        except ImportError:
            pass

    accelerator.wait_for_everyone()
