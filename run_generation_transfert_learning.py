import logging
import os
import rouge
import math
import torch
import sys
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
import wandb

import datasets
import evaluate
import nltk
import numpy as np
from statistics import mean
from utils import predict, compute_sim
from datasets import load_dataset, load_from_disk
from filelock import FileLock
from torch.utils.data import DataLoader
from codecarbon import EmissionsTracker
from BARTScore.bart_score import BARTScorer


from sentence_transformers import SentenceTransformer
import transformers
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    HfArgumentParser,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    set_seed,
    get_scheduler,
)

from nltk.translate.bleu_score import corpus_bleu
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version
from transformers.utils.versions import require_version
from peft import LoraConfig, get_peft_model, prepare_model_for_int8_training, TaskType

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.30.0.dev0")

require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/summarization/requirements.txt")

logger = logging.getLogger(__name__)

try:
    nltk.data.find("tokenizers/punkt")
except (LookupError, OSError):
    with FileLock(".lock") as lock:
        nltk.download("punkt", quiet=True)


task_name_mapping = {
    "full_to_lay_transfert_summarization": ("full_text", "lay_text"),
}

global_rouge_scorer = rouge.Rouge(metrics=['rouge-n', 'rouge-l', 'rouge-w'],
                            max_n=4,
                            limit_length=True,
                            length_limit=100,
                            length_limit_type='words',
                            apply_avg=True,
                            apply_best=False,
                            alpha=0.5,  # Default F1_score
                            weight_factor=1.2,
                            stemming=True)


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    task_name: Optional[str] = field(
        default=None,
        metadata={"help": "The name of the task to perform: " + ", ".join(task_name_mapping.keys())},
    )
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    subset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset subset to use."}
    )
    max_source_length: Optional[int] = field(
        default=1024, metadata={"help": ("The maximum total input sequence length after tokenization. Sequences longer than this will be truncated, sequences shorter will be padded.")},
    )
    max_target_length: Optional[int] = field(
        default=128, metadata={"help": ("The maximum total sequence length for target text after tokenization. Sequences longer than this will be truncated, sequences shorter will be padded.")},
    )
    num_beams: Optional[int] = field(
        default=None,metadata={"help": ("Number of beams to use for evaluation. This argument will be passed to ``model.generate``, which is used during ``evaluate`` and ``test``.")},
    )
    logging : Optional[str] = field(
        default="disabled",metadata={"help": ("Set 'disabled' to disable wandb logging, or else select logging 'online' or 'offline'")},
    )


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """
    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    use_peft: bool = field(
        default=False,
        metadata={"help": "Use PEFT for training or not"},
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where to store the pretrained models downloaded from huggingface.co"},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )

def calculate_carburacy(score, emission, beta, alpha=10):
    if emission is not None:
        normalized_score = score / 100
        score_adjustment = math.exp(math.log(normalized_score, alpha))
        return score_adjustment / (1 + emission * beta)
    return None

def get_carburacy(score, emission_train, emission_test, alpha=10, beta_train=1, beta_test=100):
    carburacy_train = calculate_carburacy(score, emission_train, beta_train, alpha)
    carburacy_test = calculate_carburacy(score, emission_test, beta_test, alpha)
    carburacy = None
    if carburacy_train is not None and carburacy_test is not None:
        carburacy = (2 * carburacy_train * carburacy_test) / (carburacy_train + carburacy_test)
    return carburacy_train, carburacy_test, carburacy


def check_for_last_checkpoint(training_args, logger):
    if os.path.isdir(training_args.output_dir):
        if training_args.do_train and not training_args.overwrite_output_dir:
            last_checkpoint = get_last_checkpoint(training_args.output_dir)
            if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
                raise ValueError(
                    f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                    "Use --overwrite_output_dir to train from scratch."
                )
            elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
                logger.info(
                    f"Checkpoint detected, resuming training at {last_checkpoint}. "
                    "To start training from scratch, change the `--output_dir` or use `--overwrite_output_dir`."
                )
            return last_checkpoint

def check_and_resize_embeddings(model, tokenizer, data_args, model_args):
    embedding_size = model.get_input_embeddings().weight.shape[0]
    if len(tokenizer) > embedding_size:
        model.resize_token_embeddings(len(tokenizer))

    if model.config.decoder_start_token_id is None:
        raise ValueError("The `config.decoder_start_token_id` is not set for the model.")

    if (hasattr(model.config, "max_position_embeddings") and 
        model.config.max_position_embeddings < data_args.max_source_length):
        if model_args.resize_position_embeddings is None or model_args.resize_position_embeddings:
            logger.warning(
                f"Resizing model's position embeddings from {model.config.max_position_embeddings} "
                f"to {data_args.max_source_length}."
            )
            model.resize_position_embeddings(data_args.max_source_length)
        else:
            error_message = (
                f"`--max_source_length` is set to {data_args.max_source_length}, but the model only supports "
                f"{model.config.max_position_embeddings} position encodings. Reduce `--max_source_length` "
                f"to {model.config.max_position_embeddings}, or use `--resize_position_embeddings` to resize."
            )
            raise ValueError(error_message)


def get_dataset_columns(raw_datasets, training_args):
    if training_args.do_train:
        if "train" not in raw_datasets:
            raise ValueError("--do_train requires a train dataset")
        return "train", raw_datasets["train"].column_names

    if training_args.do_eval:
        if "validation" not in raw_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        return "validation", raw_datasets["validation"].column_names

    if training_args.do_test:
        if "test" not in raw_datasets:
            raise ValueError("--do_test requires a test dataset")
        return "test", raw_datasets["test"].column_names

    raise AttributeError("There is nothing to do. Please pass `do_train`, `do_eval` and/or `do_test`.")




def main():
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, Seq2SeqTrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    training_args.do_test = training_args.do_predict
    training_args.output_dir = (
        f"{training_args.output_dir}/{data_args.task_name}_"
        f"{model_args.model_name_or_path.partition('/')[-1]}_"
        f"{data_args.subset_name}"
    )  

    wandb.init(mode=data_args.logging, 
               name=training_args.output_dir.split("/")[1], 
               project="sci_lay",
    )

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if training_args.should_log:
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    logger.warning(
        f"Process rank: {training_args.local_rank}, "
        f"device: {training_args.device}, "
        f"n_gpu: {training_args.n_gpu}, "
        f"distributed training: {bool(training_args.local_rank != -1)}, "
        f"16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    last_checkpoint = check_for_last_checkpoint(training_args, logger)

    set_seed(training_args.seed)

    raw_datasets = load_dataset(
        data_args.dataset_name,
        data_args.subset_name,
        cache_dir=model_args.cache_dir,
        download_mode="force_redownload",
        use_auth_token=True if model_args.use_auth_token else None,
    )

    tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_args.model_name_or_path, load_in_8bit=model_args.use_peft, cache_dir="../llms")

    check_and_resize_embeddings(model, tokenizer, data_args, model_args)

    # Preprocessing the datasets.
    # We need to tokenize inputs and targets.
    if training_args.do_train:
        if "train" not in raw_datasets:
            raise ValueError("--do_train requires a train dataset")
        column_names = raw_datasets["train"].column_names
    elif training_args.do_eval:
        if "validation" not in raw_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        column_names = raw_datasets["validation"].column_names
    elif training_args.do_test:
        if "test" not in raw_datasets:
            raise ValueError("--do_test requires a test dataset")
        column_names = raw_datasets["test"].column_names
    else:
        logger.info("There is nothing to do. Please pass `do_train`, `do_eval` and/or `do_test`.")
        return

    
    # Get the column names for input/target.
    dataset_columns = task_name_mapping.get(data_args.task_name, None)
    if data_args.text_column is None:
        text_column = dataset_columns[0] if dataset_columns is not None else column_names[0]
    else:
        text_column = data_args.text_column
        if text_column not in column_names:
            raise ValueError(
                f"--text_column' value '{data_args.text_column}' needs to be one of: {', '.join(column_names)}"
            )
    if data_args.summary_column is None:
        summary_column = dataset_columns[1] if dataset_columns is not None else column_names[1]
    else:
        summary_column = data_args.summary_column
        if summary_column not in column_names:
            raise ValueError(
                f"--summary_column' value '{data_args.summary_column}' needs to be one of: {', '.join(column_names)}"
            )

    # Temporarily set max_target_length for training.
    max_target_length = data_args.max_target_length
    padding = "max_length" if data_args.pad_to_max_length else False

    if training_args.label_smoothing_factor > 0 and not hasattr(model, "prepare_decoder_input_ids_from_labels"):
        logger.warning(
            "label_smoothing is enabled but the `prepare_decoder_input_ids_from_labels` method is not defined for"
            f"`{model.__class__.__name__}`. This will lead to loss being calculated twice and will take up more memory"
        )

    def preprocess_function(examples):
        # remove pairs where at least one record is None

        inputs, targets = [], []
        for i in range(len(examples[text_column])):
            if examples[text_column][i] and examples[summary_column][i]:
                inputs.append(examples[text_column][i])
                targets.append(examples[summary_column][i])

        inputs = [prefix + inp for inp in inputs]
        model_inputs = tokenizer(inputs, max_length=data_args.max_source_length, padding=padding, truncation=True)

        # Tokenize targets with the `text_target` keyword argument
        labels = tokenizer(text_target=targets, max_length=data_args.max_target_length, padding=padding, truncation=True)

        # If we are padding here, replace all tokenizer.pad_token_id in the labels by -100 when we want to ignore
        # padding in the loss.
        if padding == "max_length" and data_args.ignore_pad_token_for_loss:
            labels["input_ids"] = [
                [(l if l != tokenizer.pad_token_id else -100) for l in label] for label in labels["input_ids"]
            ]

        model_inputs["labels"] = labels["input_ids"]
        return model_inputs

    # Data collator
    label_pad_token_id = -100 if data_args.ignore_pad_token_for_loss else tokenizer.pad_token_id
    data_collator = DataCollatorForSeq2Seq(
        tokenizer,
        model=model,
        label_pad_token_id=label_pad_token_id,
        pad_to_multiple_of=8 if training_args.fp16 else None,
    )

    if training_args.do_train:
        train_dataset = raw_datasets["train"]

        if data_args.max_train_samples is not None:
            max_train_samples = min(len(train_dataset), data_args.max_train_samples)
            train_dataset = train_dataset.select(range(max_train_samples))
        with training_args.main_process_first(desc="train dataset map pre-processing"):
            train_dataset = train_dataset.map(
                preprocess_function,
                batched=True,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=column_names,
                load_from_cache_file=not data_args.overwrite_cache,
                desc="Running tokenizer on train dataset",
            )
        
        # Optimizer
        # Split weights in two groups, one with weight decay and the other not.
        no_decay = ["bias", "LayerNorm.weight", "layer_norm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": training_args.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

        train_dataloader = DataLoader(
            train_dataset, shuffle=True, collate_fn=data_collator,
            batch_size=training_args.per_device_train_batch_size
        )
        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=training_args.learning_rate)
        num_update_steps_per_epoch = len(train_dataloader)
        max_train_steps = training_args.num_train_epochs * num_update_steps_per_epoch

        lr_scheduler = get_scheduler(
            name="linear",
            optimizer=optimizer,
            num_warmup_steps=0,
            num_training_steps=max_train_steps
        )
        optimizers = (optimizer, lr_scheduler)
    else:
        optimizers = (None, None)

    if training_args.do_eval:
        max_target_length = data_args.val_max_target_length
        eval_dataset = raw_datasets["validation"]
        
        if data_args.max_eval_samples is not None:
            max_eval_samples = min(len(eval_dataset), data_args.max_eval_samples)
            eval_dataset = eval_dataset.select(range(max_eval_samples))
        with training_args.main_process_first(desc="validation dataset map pre-processing"):
            eval_dataset = eval_dataset.map(
                preprocess_function,
                batched=True,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=column_names,
                load_from_cache_file=not data_args.overwrite_cache,
                desc="Running tokenizer on validation dataset",
            )

    if training_args.do_test:
        max_target_length = data_args.val_max_target_length
        test_dataset = raw_datasets["test"]
            
        if data_args.max_test_samples is not None:
            max_test_samples = min(len(test_dataset), data_args.max_test_samples)
            test_dataset = test_dataset.select(range(max_test_samples))
        with training_args.main_process_first(desc="test dataset map pre-processing"):
            test_dataset = test_dataset.map(
                preprocess_function,
                batched=True,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=column_names,
                load_from_cache_file=not data_args.overwrite_cache,
                desc="Running tokenizer on test dataset",
            )

    # Metric
    metric_bertscore = evaluate.load("bertscore")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    bart_scorer = BARTScorer(device=device, checkpoint='facebook/bart-large-cnn')
    sim_model = SentenceTransformer('sentence-transformers/roberta-large-nli-stsb-mean-tokens').to(device)


    def compute_metrics(eval_preds):
        preds, labels, _ = eval_preds
        if isinstance(preds, tuple):
            preds = preds[0]
       
        # Replace -100s used for padding as we can't decode them
        preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
        labels = np.where(labels != -100, labels, tokenizer.pad_token_id)

        decoded_preds = [pred.strip() for pred in tokenizer.batch_decode(preds, skip_special_tokens=True)]
        decoded_labels = [label.strip() for label in tokenizer.batch_decode(labels, skip_special_tokens=True)]

        rouge_scores = global_rouge_scorer.get_scores(hypothesis=decoded_preds, references=decoded_labels)
        result = {"rouge1": round(100 * rouge_scores["rouge-1"]["f"], 2),
                  "rouge2": round(100 * rouge_scores["rouge-2"]["f"], 2),
                  "rougeL": round(100 * rouge_scores["rouge-l"]["f"], 2),
                }
        
        # Compute BLEU scores
        tokenized_predictions = [prediction.split(" ") for prediction in decoded_preds]
        tokenized_labels = [[label.split(" ")] for label in decoded_labels]
        result["bleu1"] = round(100 * corpus_bleu(tokenized_labels, tokenized_predictions, weights=(1, 0, 0, 0)), 2)
        result["bleu2"] = round(100 * corpus_bleu(tokenized_labels, tokenized_predictions, weights=(1/2, 1/2, 0, 0)), 2)
        result["bleu3"] = round(100 * corpus_bleu(tokenized_labels, tokenized_predictions, weights=(1/3, 1/3, 1/3, 0)), 2)
        result["bleu4"] = round(100 * corpus_bleu(tokenized_labels, tokenized_predictions, weights=(1/4, 1/4, 1/4, 1/4)), 2)
        

        result["R"] = round(np.mean([result["rouge1"], result["rouge2"], result["rougeL"]]) / \
            (1 + (np.var([result["rouge1"]/100, result["rouge2"]/100, result["rougeL"]/100]))), 2)

        result_bs = metric_bertscore.compute(predictions=decoded_preds, references=decoded_labels, lang="en",
                                             idf=True, rescale_with_baseline=True,
                                             model_type=model_args.model_for_bertscore)
        result["bertscore"] = round(sum(result_bs["f1"]) / len(result_bs["f1"]) * 100, 2)

        bartr_scores = bart_scorer.score(decoded_preds, decoded_labels)
        bartp_scores = bart_scorer.score(decoded_labels, decoded_preds)

        bart_score_R = mean(bartr_scores)
        bart_score_P = mean(bartp_scores)
        bart_score_F = mean([mean([pscore, rscore]) for pscore, rscore in zip(bartp_scores, bartr_scores)])
        result["bart_score_R"] = round(bart_score_R, 3)
        result["bart_score_P"] = round(bart_score_P, 3)
        result["bart_score_F"] = round(bart_score_F, 3)
        
        result["mean_cos_sim"] = compute_sim(sim_model, decoded_labels, decoded_preds)

        result["gen_len"] = np.mean([np.count_nonzero(pred != tokenizer.pad_token_id) for pred in preds])

        return result
    
    # Override the decoding parameters of Seq2SeqTrainer
    training_args.generation_max_length = (
        training_args.generation_max_length
        if training_args.generation_max_length is not None
        else data_args.val_max_target_length
    )
    training_args.generation_num_beams = (
        data_args.num_beams if data_args.num_beams is not None else training_args.generation_num_beams
    )


    # Initialize our Trainer
    trainer = Seq2SeqTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        tokenizer=tokenizer,
        data_collator=data_collator,
        compute_metrics=compute_metrics if training_args.predict_with_generate else None,
        optimizers=optimizers,
    )

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint

        train_tracker = EmissionsTracker(measure_power_secs=100000, save_to_file=False)
        train_tracker.start()
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        train_emissions = train_tracker.stop()
        trainer.save_model()  # Saves the tokenizer too for easy upload

        metrics = train_result.metrics
        max_train_samples = (
            data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
        )
        metrics["train_samples"] = min(max_train_samples, len(train_dataset))
        metrics["train_emissions"] = train_emissions

        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
    else:
        train_emissions = None

    # Predictions on validation set
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        max_eval_samples = (
        data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
        )
        predict(trainer, eval_dataset, max_eval_samples, training_args, tokenizer, train_emissions, "eval")

    # Predictions on test set
    if training_args.do_test:
        logger.info("*** Test ***")
        max_test_samples = (
        data_args.max_test_samples if data_args.max_test_samples is not None else len(test_dataset)
        )

        predict(trainer, test_dataset, max_test_samples, training_args, tokenizer, train_emissions, "test")


    kwargs = {"finetuned_from": model_args.model_name_or_path, "tasks": data_args.task_name}
    if data_args.task_name is not None:
        kwargs["dataset_tags"] = data_args.task_name
        if data_args.dataset_config_name is not None:
            kwargs["dataset_args"] = data_args.dataset_config_name
            kwargs["dataset"] = f"{data_args.task_name} {data_args.dataset_config_name}"
        else:
            kwargs["dataset"] = data_args.task_name

    if training_args.push_to_hub:
        trainer.push_to_hub(**kwargs)
    else:
        trainer.create_model_card(**kwargs)