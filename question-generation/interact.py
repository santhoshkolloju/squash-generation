# # Copyright (c) 2019-present, HuggingFace Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import json
import logging
import random
import tqdm
from argparse import ArgumentParser
from itertools import chain
from pprint import pformat
import copy
import torch
import torch.nn.functional as F

from pytorch_pretrained_bert import OpenAIGPTLMHeadModel, OpenAIGPTTokenizer, GPT2LMHeadModel, GPT2Tokenizer
from train import SPECIAL_TOKENS, build_input_from_segments
from dataloader import get_dataset_from_file


def top_filtering(logits, top_k=0, top_p=0.0, threshold=-float('Inf'), filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k, top-p (nucleus) and/or threshold filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k: <=0: no filtering, >0: keep only top k tokens with highest probability.
            top_p: <=0.0: no filtering, >0.0: keep only a subset S of candidates, where S is the smallest subset
                whose total probability mass is greater than or equal to the threshold top_p.
                In practice, we select the highest probability tokens whose cumulative probability mass exceeds
                the threshold top_p.
            threshold: a minimal threshold to keep logits
    """
    assert logits.dim() == 1  # Only work for batch size 1 for now - could update but it would obfuscate a bit the code
    top_k = min(top_k, logits.size(-1))
    if top_k > 0:
        # Remove all tokens with a probability less than the last token in the top-k tokens
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        # Compute cumulative probabilities of sorted tokens
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probabilities = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probabilities > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        # Back to unsorted indices and set them to -infinity
        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value

    indices_to_remove = logits < threshold
    logits[indices_to_remove] = filter_value

    return logits


def sample_sequence(inst, tokenizer, model, args,past):
    special_tokens_ids = tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)
    inst['original_question'] = inst['question']
    inst['question'] = []
    inst['orig_paragraph'] = inst['paragraph']
    inst['paragraph'] = []
    instance, _ = build_input_from_segments(inst, tokenizer, with_eos=False)
    input_ids = torch.tensor(instance['input_ids'][1:],device=args.device).unsqueeze(0) #remove extra eos
    token_type_ids = torch.tensor(instance['token_type_ids'][1:],device=args.device).unsqueeze(0)
    inst['original_context'] = instance['input_ids']
    logits, past = model(input_ids, token_type_ids=token_type_ids,past=past)
    token_type = instance['token_type_ids'][-1]

    for i in range(args.max_length):
        #instance, _ = build_input_from_segments(inst, tokenizer, with_eos=False)

        #input_ids = torch.tensor(instance["input_ids"], device=args.device).unsqueeze(0)
        #token_type_ids = torch.tensor(instance["token_type_ids"], device=args.device).unsqueeze(0)

        

        logits = logits[0, -1, :] / args.temperature
        logits = top_filtering(logits, top_k=args.top_k, top_p=args.top_p)
        probs = F.softmax(logits, dim=-1)

        prev = torch.topk(probs, 1)[1] if args.no_sample else torch.multinomial(probs, 1)
        if i < args.min_length and prev.item() in special_tokens_ids:
            while prev.item() in special_tokens_ids:
                prev = torch.multinomial(probs, num_samples=1)

        if prev.item() in special_tokens_ids:
            break

        input_ids = prev.unsqueeze(0)
        token_type_ids = torch.tensor([token_type]).unsqueeze(0)
        logits, past = model(input_ids, token_type_ids=token_type_ids,past=past)   
        inst['question'].append(prev.item())
    inst['paragraph'] = inst['orig_paragraph']
    return inst


def run():
    parser = ArgumentParser()
    parser.add_argument("--model_type", type=str, default="gpt", help="gpt or gpt2")
    parser.add_argument("--model_checkpoint", type=str, default="", help="Path, url or short name of the model")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device (cuda or cpu)")
    parser.add_argument("--filename", type=str, default="data/instances_dev.pkl", help="File to use for decoding")
    parser.add_argument("--no_sample", action='store_true', help="Set to use greedy decoding instead of sampling")
    parser.add_argument("--max_length", type=int, default=50, help="Maximum length of the output utterances")
    parser.add_argument("--min_length", type=int, default=1, help="Minimum length of the output utterances")
    parser.add_argument("--seed", type=int, default=42, help="Seed")
    parser.add_argument("--temperature", type=int, default=0.7, help="Sampling softmax temperature")
    parser.add_argument("--top_k", type=int, default=0, help="Filter top-k tokens before sampling (<=0: no filtering)")
    parser.add_argument("--top_p", type=float, default=0.9, help="Nucleus filtering (top-p) before sampling (<=0.0: no filtering)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__file__)
    logger.info(pformat(args))

    random.seed(args.seed)
    torch.random.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    logger.info("Get pretrained model and tokenizer")

    if args.model_type == 'gpt2':
        tokenizer = GPT2Tokenizer.from_pretrained(args.model_checkpoint)
        model = GPT2LMHeadModel.from_pretrained(args.model_checkpoint)
    else:
        tokenizer = OpenAIGPTTokenizer.from_pretrained(args.model_checkpoint)
        model = OpenAIGPTLMHeadModel.from_pretrained(args.model_checkpoint)

    model.to(args.device)
    model.eval()

    data = get_dataset_from_file(tokenizer, args.filename)
    final_output_dict = {
        "version": "squash-2.0",
        "data": [{
            "paragraphs": []
        }]
    }
    question_number = 0
    prev_para_index = None
    past =None

    for inst in tqdm.tqdm(data):
        with torch.no_grad():
            para_index = inst['para_index']
            if para_index != prev_para_index:
                past  = None
                instance1 = copy.deepcopy(inst)
                instance1['question'] = []
                instance1['ori_answer'] = instance1['answer']
                instance1['answer'] = []
                instance, _ = build_input_from_segments(inst, tokenizer, with_eos=False)
                input_ids = torch.tensor(instance['input_ids'][:-2],device=args.device).unsqueeze(0) #remove extra eos
                token_type_ids = torch.tensor(instance['token_type_ids'][:-2],device=args.device).unsqueeze(0)
                _ , past = model(input_ids, token_type_ids=token_type_ids,past=past)

            output = sample_sequence(inst, tokenizer, model, args,past)

        original_paragraph = tokenizer.decode(output['paragraph'])
        generated_question = tokenizer.decode(output['question'], skip_special_tokens=True)
        original_answer = tokenizer.decode(output['answer'], skip_special_tokens=True)
        para_index = inst['para_index']
        prev_para_index = inst['para_index']

        # Output in a SQUAD-like format with questions clumped together under their parent paragraph
        if len(final_output_dict["data"][0]["paragraphs"]) > para_index:
            # verify whether the paragraph text is identical
            assert original_paragraph == final_output_dict["data"][0]["paragraphs"][para_index]['context']
            # append the question answer pair
            final_output_dict["data"][0]["paragraphs"][para_index]['qas'].append({
                'id': 'question_%d' % question_number,
                'question': generated_question,
                'answers': [{
                    'text': original_answer,
                    'answer_start': original_paragraph.index(original_answer)
                }],
                'class': output['class'],
                'algorithm': output['algorithm'],
                'is_impossible': False
            })
        else:
            # add a new question to the list of QA pairs
            final_output_dict['data'][0]['paragraphs'].append({
                'context': original_paragraph,
                'qas': [{
                    'id': 'question_%d' % question_number,
                    'question': generated_question,
                    'answers': [{
                        'text': original_answer,
                        'answer_start': original_paragraph.index(original_answer)
                    }],
                    'class': output['class'],
                    'algorithm': output['algorithm'],
                    'is_impossible': False
                }]
            })

        question_number += 1

    with open("squash/temp/generated_questions.json", "w") as f:
        f.write(json.dumps(final_output_dict))


if __name__ == "__main__":
    run()
