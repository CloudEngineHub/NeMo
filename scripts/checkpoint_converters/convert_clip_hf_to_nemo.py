# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
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

"""
Usage example:
    torchrun --nproc-per-node=1 /opt/NeMo/scripts/checkpoint_converters/convert_clip_hf_to_nemo.py \
        --input_name_or_path=openai/clip-vit-large-patch14 \
        --output_path=openai_clip.nemo \
        --hparams_file=/opt/NeMo/examples/multimodal/vision_language_foundation/clip/conf/megatron_clip_VIT-L-14.yaml

Additionally, provide a NeMo hparams file with the correct model architecture arguments. Refer to examples/multimodal/foundation/clip/conf/megatron_clip_config.yaml.

After conversion, you can verify with the following command:

  wget https://upload.wikimedia.org/wikipedia/commons/0/0f/1665_Girl_with_a_Pearl_Earring.jpg
  torchrun --nproc-per-node=1 /opt/NeMo/examples/multimodal/vision_language_foundation/clip/megatron_clip_infer.py \
    model.restore_from_path=./openai_clip.nemo \
    image_path=./1665_Girl_with_a_Pearl_Earring.jpg \
    texts='["a dog", "a boy", "a girl"]'

It should generate a high probability for "a girl" tag, e.g.
Given image's CLIP text probability:  [('a dog', 0.0049710185), ('a boy', 0.002258187), ('a girl', 0.99277073)]

"""

import os
from argparse import ArgumentParser

import torch
from lightning.pytorch.plugins.environments import TorchElasticEnvironment
from lightning.pytorch.trainer.trainer import Trainer
from omegaconf import OmegaConf
from transformers import CLIPModel

from nemo.collections.multimodal.models.vision_language_foundation.clip.megatron_clip_models import MegatronCLIPModel
from nemo.utils import AppState, logging
from nemo.utils.distributed import initialize_distributed

try:
    from megatron.core import parallel_state

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False


def get_args():
    parser = ArgumentParser()
    parser.add_argument("--input_name_or_path", type=str, default="openai/clip-vit-base-patch32")

    parser.add_argument(
        "--hparams_file",
        type=str,
        default=None,
        required=True,
        help="Path config for restoring. It's created during training and may need to be modified during restore if restore environment is different than training. Ex: /opt/NeMo/examples/multimodal/vision_language_foundation/clip/conf/megatron_clip_VIT-L-14.yaml",
    )
    parser.add_argument("--output_path", type=str, default=None, required=True, help="Path to output .nemo file.")

    parser.add_argument("--gpus_per_node", type=int, required=False, default=1)
    parser.add_argument("--tensor_model_parallel_size", type=int, required=False, default=1)
    parser.add_argument("--pipeline_model_parallel_size", type=int, required=False, default=1)
    parser.add_argument(
        "--pipeline_model_parallel_split_rank",
        type=int,
        required=False,
        default=None,
        help="If pipeline parallel size > 1, this is the rank at which the encoder ends and the decoder begins.",
    )
    parser.add_argument("--local_rank", type=int, required=False, default=os.getenv('LOCAL_RANK', -1))

    args = parser.parse_args()
    return args


def mapping_hf_state_dict(hf_model):
    hf_state_dict = hf_model.state_dict()
    hf_config = hf_model.config
    key_mapping = {
        "text_projection.weight": "text_encoder.head.weight",
        "visual_projection.weight": "vision_encoder.head.weight",
    }

    layer_mapping = {
        ".layer_norm1.weight": ".self_attention.linear_qkv.layer_norm_weight",
        ".layer_norm1.bias": ".self_attention.linear_qkv.layer_norm_bias",
        ".layer_norm2.weight": ".mlp.linear_fc1.layer_norm_weight",
        ".layer_norm2.bias": ".mlp.linear_fc1.layer_norm_bias",
        ".self_attn.out_proj.weight": ".self_attention.linear_proj.weight",
        ".self_attn.out_proj.bias": ".self_attention.linear_proj.bias",
        ".mlp.fc1.weight": ".mlp.linear_fc1.weight",
        ".mlp.fc1.bias": ".mlp.linear_fc1.bias",
        ".mlp.fc2.weight": ".mlp.linear_fc2.weight",
        ".mlp.fc2.bias": ".mlp.linear_fc2.bias",
        ".pre_layrnorm.weight": ".ln_pre.weight",
        ".pre_layrnorm.bias": ".ln_pre.bias",
        ".post_layernorm.weight": ".final_layernorm.weight",
        ".post_layernorm.bias": ".final_layernorm.bias",
        ".embeddings.patch_embedding.weight": ".conv1.weight",
        ".embeddings.class_embedding": ".class_token",
        ".final_layer_norm.weight": ".final_layernorm.weight",
        ".final_layer_norm.bias": ".final_layernorm.bias",
        ".embeddings.token_embedding.weight": ".embedding.word_embeddings.weight",
        "vision_encoder.embeddings.position_embedding.weight": "vision_encoder.position_embeddings.weight",
        "text_encoder.embeddings.position_embedding.weight": "text_encoder.embedding.position_embeddings.weight",
    }

    nemo_state_dict = {}
    for key in hf_state_dict.keys():
        if key.startswith("text_model.encoder.layers"):
            key_ = key.replace("text_model.encoder.layers", "text_encoder.decoder.layers")
        elif key.startswith("vision_model.encoder.layers"):
            key_ = key.replace("vision_model.encoder.layers", "vision_encoder.decoder.layers")
        elif key.startswith('vision_model.'):
            key_ = key.replace("vision_model.", "vision_encoder.")
        elif key.startswith('text_model.'):
            key_ = key.replace('text_model.', 'text_encoder.')
        else:
            key_ = key
        for pat in key_mapping:
            if key_ == pat:
                key_ = key_.replace(pat, key_mapping[pat])
        for pat in layer_mapping:
            if key_.endswith(pat):
                key_ = key_[: -len(pat)] + layer_mapping[pat]
                break
        if "vision" in key_:
            config = hf_config.vision_config
        else:
            config = hf_config.text_config
        head_num = num_query_groups = config.num_attention_heads
        hidden_size = config.hidden_size
        head_size = hidden_size // head_num
        heads_per_group = head_num // num_query_groups

        if 'q_proj.weight' in key_:
            key_k = key.replace('q_proj', 'k_proj')
            key_v = key.replace('q_proj', 'v_proj')
            key_new = key_.replace('self_attn.q_proj', 'self_attention.linear_qkv')
            q_weight, k_weight, v_weight = hf_state_dict[key], hf_state_dict[key_k], hf_state_dict[key_v]

            q_weight = q_weight.reshape(head_num, head_size, hidden_size)
            k_weight = k_weight.reshape(num_query_groups, head_size, hidden_size)
            v_weight = v_weight.reshape(num_query_groups, head_size, hidden_size)
            qkv_weight = torch.empty((0, head_size, hidden_size), device=q_weight.device)
            for i in range(num_query_groups):
                qkv_weight = torch.cat((qkv_weight, q_weight[i * heads_per_group : (i + 1) * heads_per_group, :, :]))
                qkv_weight = torch.cat((qkv_weight, k_weight[i : i + 1, :, :]))
                qkv_weight = torch.cat((qkv_weight, v_weight[i : i + 1, :, :]))
            qkv_weight = qkv_weight.reshape([head_size * (head_num + 2 * num_query_groups), hidden_size])
            nemo_state_dict[key_new] = qkv_weight

        elif 'q_proj.bias' in key_:
            key_k = key.replace('q_proj', 'k_proj')
            key_v = key.replace('q_proj', 'v_proj')
            key_new = key_.replace('self_attn.q_proj', 'self_attention.linear_qkv')
            q_bias, k_bias, v_bias = hf_state_dict[key], hf_state_dict[key_k], hf_state_dict[key_v]

            q_bias = q_bias.reshape(head_num, head_size)
            k_bias = k_bias.reshape(num_query_groups, head_size)
            v_bias = v_bias.reshape(num_query_groups, head_size)
            qkv_bias = torch.empty((0, head_size), device=q_bias.device)
            for i in range(num_query_groups):
                qkv_bias = torch.cat((qkv_bias, q_bias[i * heads_per_group : (i + 1) * heads_per_group, :]))
                qkv_bias = torch.cat((qkv_bias, k_bias[i : i + 1, :]))
                qkv_bias = torch.cat((qkv_bias, v_bias[i : i + 1, :]))
            qkv_bias = qkv_bias.reshape([head_size * (head_num + 2 * num_query_groups)])
            nemo_state_dict[key_new] = qkv_bias
        elif not ('k_proj' in key_ or 'v_proj' in key_ or 'position_ids' in key_):
            nemo_state_dict[key_] = hf_state_dict[key]

    nemo_state_dict["vision_encoder.class_token"] = nemo_state_dict["vision_encoder.class_token"].reshape(1, 1, -1)

    return nemo_state_dict


def convert(local_rank, rank, world_size, args):
    app_state = AppState()
    app_state.data_parallel_rank = 0
    num_nodes = world_size // args.gpus_per_node
    trainer = Trainer(
        devices=args.gpus_per_node, num_nodes=num_nodes, accelerator='gpu', plugins=[TorchElasticEnvironment()]
    )

    app_state.pipeline_model_parallel_size = args.pipeline_model_parallel_size
    app_state.tensor_model_parallel_size = args.tensor_model_parallel_size

    # no use atm, use to split ranks in encoder/decoder models.
    if args.pipeline_model_parallel_size > 1 and args.model_type in []:
        if args.pipeline_model_parallel_split_rank is not None:
            app_state.pipeline_model_parallel_split_rank = args.pipeline_model_parallel_split_rank
        else:
            if args.pipeline_model_parallel_size % 2 != 0:
                raise ValueError(
                    f"Pipeline model parallel size {args.pipeline_model_parallel_size} must be even if split rank is not specified."
                )
            else:
                # If split rank is not set, then we set it to be pipeline_model_parallel_size // 2 - this is because in most cases we have the same number of enc/dec layers.
                app_state.pipeline_model_parallel_split_rank = args.pipeline_model_parallel_size // 2
    else:
        app_state.pipeline_model_parallel_split_rank = None

    app_state.model_parallel_size = app_state.tensor_model_parallel_size * app_state.pipeline_model_parallel_size

    parallel_state.initialize_model_parallel(
        tensor_model_parallel_size=app_state.tensor_model_parallel_size,
        pipeline_model_parallel_size=app_state.pipeline_model_parallel_size,
    )

    app_state.pipeline_model_parallel_rank = parallel_state.get_pipeline_model_parallel_rank()
    app_state.tensor_model_parallel_rank = parallel_state.get_tensor_model_parallel_rank()

    cfg = OmegaConf.load(args.hparams_file)
    cfg.model.mcore_gpt = True
    cfg.model.transformer_engine = True
    cfg.model.text.position_embedding_type = "learned_absolute"
    cfg.model.vision.position_embedding_type = "learned_absolute"

    model = MegatronCLIPModel(cfg.model, trainer)

    hf_model = CLIPModel.from_pretrained(args.input_name_or_path)
    state_dict = mapping_hf_state_dict(hf_model)

    model.model.load_state_dict(state_dict, strict=False)

    model.save_to(args.output_path)

    logging.info(f'NeMo model saved to: {args.output_path}')


if __name__ == '__main__':
    args = get_args()
    local_rank, rank, world_size = initialize_distributed(args)
    convert(local_rank, rank, world_size, args)
