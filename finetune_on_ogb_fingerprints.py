import math
import wandb
import argparse
import json
from copy import deepcopy
import numpy as np

import datamol as dm

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchsummary import summary
from torch.utils.data import DataLoader, Dataset
from sklearn.metrics import roc_auc_score, average_precision_score, r2_score, mean_absolute_error
from scipy.stats import spearmanr

from ogb.graphproppred import PygGraphPropPredDataset, Evaluator
import pandas as pd

SEEDS = [345374, 467039, 986009, 916060, 641316, 798438, 665204, 373079, 228395, 935414]


# model stuff
def train_one_epoch(model, dataloader, loss_fn, optimizer, task_type, epoch, fold):
    model.train()
    total_loss = 0
    for inputs, targets in dataloader:

        # Filter samples with NaNs out
        nan_mask = ~torch.isnan(inputs).any(dim=1)
        filtered_inputs = inputs[nan_mask]
        filtered_targets = targets[nan_mask]

        if len(filtered_inputs) > 0:
            optimizer.zero_grad()
            outputs = model(filtered_inputs.float())

            if task_type == "classification":
                filtered_targets = filtered_targets.long()
            elif task_type == "multi-class":
                filtered_targets = filtered_targets.float()
            else:
                filtered_targets = filtered_targets.float()

            if task_type == "multi-class":
                nan_mask = (filtered_targets == filtered_targets)
                loss = loss_fn(outputs[nan_mask].squeeze(), filtered_targets[nan_mask].squeeze())
            else:
                loss = loss_fn(outputs.squeeze(), filtered_targets)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

    loss = total_loss / len(dataloader)
    wandb.log({'epoch': epoch + fold, 'train_loss': loss})
    print(f"## Epoch {epoch+1} - Train Loss: {loss:.4f}")
    return model

def evaluate(model, dataloader, loss_fn, task_type, evaluation_type, epoch, fold, evaluator):
    model.eval()
    total_loss = 0
    all_outputs = []  # For regression, store raw outputs
    all_probs = []    # For classification, store probabilities
    all_targets = []

    with torch.no_grad():
        for inputs, targets in dataloader:
            outputs = model(inputs.float())

            if task_type == "classification":
                loss_targets = targets.long()
            elif task_type == "multi-class":
                loss_targets = targets.float()
            else:
                loss_targets = targets.float()

            if task_type == "multi-class":
                nan_mask = (targets == targets)
                loss = loss_fn(outputs[nan_mask].squeeze(), loss_targets[nan_mask].squeeze())
            else:
                loss = loss_fn(outputs, loss_targets)

            total_loss += loss.item()

            if task_type == 'classification':
                probs = torch.softmax(outputs, dim=1)[:, 1]
                all_probs.extend(probs.tolist())
                all_targets.extend(targets.tolist())

            elif task_type == "multi-class":
                all_targets.append(targets.view(outputs.shape))
                all_outputs.append(outputs)
            else:
                # Ensure outputs are always in list format
                outputs = outputs.squeeze()
                if outputs.dim() == 0:  # Check if outputs is a scalar
                    all_outputs.extend(outputs.unsqueeze(0).tolist())  # Append scalar directly
                else:
                    all_outputs.extend(outputs.tolist())  # Extend list

                all_targets.extend(targets.tolist())

    loss = total_loss / len(dataloader)
    metrics = {f'{evaluation_type}_loss': loss}

    if task_type == 'classification':
        # Filter out NaNs
        clean_indices = [i for i, x in enumerate(all_probs) if not np.isnan(x)]
        all_probs = [all_probs[i] for i in clean_indices]
        all_targets = [all_targets[i] for i in clean_indices]
        
        auroc = roc_auc_score(all_targets, all_probs)
        avpr = average_precision_score(all_targets, all_probs) # apparently same as AUPRC
        metrics.update({
            f'{evaluation_type}_auroc': auroc,
            f'{evaluation_type}_avpr': avpr,
        })
    elif task_type == "multi-class":
        auroc = evaluator.eval(dict(
            y_true = torch.cat(all_targets),
            y_pred = torch.cat(all_outputs)
        ))["rocauc"]
        metrics.update({
            f'{evaluation_type}_auroc': auroc,
        })
    else:
        # Filter out NaNs
        clean_indices = [i for i, x in enumerate(all_outputs) if not np.isnan(x)]
        all_outputs = [all_outputs[i] for i in clean_indices]
        all_targets = [all_targets[i] for i in clean_indices]

        r2 = r2_score(all_targets, all_outputs)
        mae = mean_absolute_error(all_targets, all_outputs)
        spearman_corr, _ = spearmanr(all_targets, all_outputs)
        metrics.update({
            f'{evaluation_type}_r2': r2,
            f'{evaluation_type}_mae': mae,
            f'{evaluation_type}_spearman': spearman_corr,   
        })

    if evaluation_type == 'val':
        wandb.log({**metrics, 'epoch': epoch + fold})
    else:
        wandb.log({**metrics, 'fold': fold})

    print(json.dumps(metrics, indent=5))
    
    return metrics

class Model(nn.Module):
    def __init__(self, input_dim, depth=3, hidden_dim=512, activation_fn='relu', combine_input='concat', num_classes=None, num_tasks=0, dropout_rate=0.1, **kwargs):
        super(Model, self).__init__()

        if depth < 2:
            raise ValueError("Depth must be at least 2")

        if depth == 2 and combine_input == 'concat' and hidden_dim != input_dim:
            raise ValueError("When depth is 2 and combine_input is 'concat', hidden_dim must match input_dim")

        self.depth = depth
        self.hidden_dim = hidden_dim
        self.combine_input = combine_input
        self.dropout = nn.Dropout(dropout_rate)
        self.layers = nn.ModuleList()
        self.batch_norms = nn.ModuleList()  # Batch normalization layers

        # Determine activation function
        if activation_fn == 'relu':
            self.activation_fn = F.relu
        else:
            raise NotImplementedError(f"Activation function {activation_fn} not implemented.")

        # Create layers and batch normalization layers
        for i in range(depth):
            if i == 0: # first layer
                in_dim = input_dim
                out_dim = hidden_dim
            elif i == depth - 1: # last layer
                in_dim = input_dim + hidden_dim if self.combine_input == 'concat' else hidden_dim

                if num_tasks == 0:
                    out_dim = num_classes if num_classes is not None else 1
                else:
                    out_dim = num_tasks
            else: # in between layers
                in_dim = hidden_dim
                out_dim = hidden_dim
            self.layers.append(nn.Linear(in_dim, out_dim))
            self.batch_norms += [nn.BatchNorm1d(hidden_dim)] if i != depth - 1 else []

    def forward(self, x):
        original_x = x
        for i in range(self.depth):
            x = self.layers[i](x)
            if i < self.depth - 1:
                x = self.batch_norms[i](x)
                x = self.activation_fn(x)
                x = self.dropout(x)

            if self.combine_input == 'concat' and i == self.depth - 2:
                x = torch.cat((x, original_x), dim=1)

        if x.shape[1] == 1:  # If final output dimension is 1, squeeze it for regression
            x = x.squeeze(1)

        return x


# factories
def dataloader_factory(split_name, benchmark, split_idx, i2v, args, seed=42):
    assert split_name == 'train+val' or split_name == 'test', "Wrong value for `split_name` argument passed to dataloader_factory"

    def match_and_replace_input_column(samples_df):
        transformed_df = samples_df.copy()
        transformed_df["smiles"] = transformed_df["smiles"].apply(
            lambda s: i2v[dm.unique_id(s)].detach().numpy())
        return transformed_df

    class SingleInstancePredictionDataset(Dataset):
        def __init__(self, samples_df, task_type):
            self.samples = samples_df["smiles"].tolist()

            target_columns = [col for col in samples_df.columns.values if not col in ["smiles", "mol_id"]]
            self.targets = zip(*[samples_df[col].tolist() for col in target_columns])
            if task_type == "multi-class":
                self.targets = [[float(tgt) for tgt in target] for target in self.targets]
                self.out_dim = len(target_columns)
            elif task_type == "classification":
                assert len(target_columns) == 1
                self.targets = samples_df[target_columns[0]].tolist()
                self.targets = [float(target) for target in self.targets]
                self.out_dim = 0

        def __len__(self):
            return len(self.samples)

        def __getitem__(self, idx):
            sample = torch.tensor(self.samples[idx])
            target = torch.tensor(self.targets[idx])
            return sample, target

    train_loader, val_loader, test_loader, input_dim, output_dim = None, None, None, None, None

    if split_name == 'train+val':
        train_split, val_split =  benchmark.iloc[split_idx["train"].tolist()], benchmark.iloc[split_idx["valid"].tolist()]

        train_samples = match_and_replace_input_column(train_split)
        val_samples = match_and_replace_input_column(val_split)
        
        train_dataset = SingleInstancePredictionDataset(train_samples, args.task_type)
        val_dataset = SingleInstancePredictionDataset(val_samples, args.task_type)

        train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, drop_last=True)
        val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)
    else:
        test_split = benchmark.iloc[split_idx["test"].tolist()]
        test_samples = match_and_replace_input_column(test_split)

        test_dataset = SingleInstancePredictionDataset(test_samples, args.task_type)

        test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False)

        input_dim = test_samples["smiles"].iloc[0].shape[0]
        output_dim = test_dataset.out_dim

    return train_loader, val_loader, test_loader, input_dim, output_dim

def model_factory(args):
    model = Model(**vars(args))

    if args.task_type == "classification":
        loss_fn = nn.CrossEntropyLoss()
    elif args.task_type == "multi-class":
        loss_fn = F.binary_cross_entropy_with_logits
    else:
        loss_fn = nn.MSELoss()

    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    summary(model, input_size=(args.input_dim,), batch_size=args.batch_size)
    return model, loss_fn, optimizer, trainable_params





# optimiser stuff
def l1_regularization(model, scale):
    l1_loss = torch.tensor(0.0, requires_grad=True)
    for param in model.parameters():
        l1_loss += torch.norm(param, 1)
    return scale * l1_loss

def adjust_learning_rate(optimizer, epoch, args):
    if epoch < args.warmup_epochs:
        # Linear warmup
        lr = args.lr * (epoch + 1) / args.warmup_epochs
    elif args.lr_schedule == 'constant':
        lr = args.lr
    elif args.lr_schedule == 'linear':
        # Linear decay
        lr = args.lr * (1 - (epoch - args.warmup_epochs) / (args.epochs - args.warmup_epochs))
    elif args.lr_schedule == 'cosine':
        # Cosine decay
        lr = args.lr * (1 + math.cos(math.pi * (epoch - args.warmup_epochs) / (args.epochs - args.warmup_epochs))) / 2

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    current_lr = optimizer.param_groups[0]['lr']
    wandb.log({'epoch': epoch, 'lr_at_epoch': current_lr})

    
def aggregate_dicts(dicts):
    aggr_dict = {}
    for d in dicts:
        for key, value in d.items():
            # Ensure value is not a list to avoid nesting
            if not isinstance(value, list):
                if key in aggr_dict:
                    aggr_dict[key].append(value)
                else:
                    aggr_dict[key] = [value]
            else:
                # Handle the case where the value is already a list
                if key in aggr_dict:
                    aggr_dict[key].extend(value)
                else:
                    aggr_dict[key] = value
    return aggr_dict

def calculate_statistics(aggr_dict):
    result = {}
    for key, values in aggr_dict.items():
        min_val = min(values)
        max_val = max(values)
        mean_val = sum(values) / len(values) if values else 0
        variance = sum((x - mean_val) ** 2 for x in values) / len(values) if len(values) > 1 else 0
        std_val = variance ** 0.5
        result[key] = {'min': min_val, 'max': max_val, 'mean': mean_val, 'std': std_val}
    return result



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-name', type=str, default='default-model', help='Name of model, is used to construct a name for the wandb run')
    parser.add_argument('--fingerprints-path', type=str, default='ogb-results/ids_to_fingerprint.pt', help='Path to ids_to_fingerprint.pt')
    parser.add_argument('--dataset', type=str, default='ogbg-molbace', help='Name of the benchmark from admet_group')
    parser.add_argument('--epochs', type=int, default=5, help='Number of training epochs')
    parser.add_argument('--split', type=float, default=0.1, help='Ratio of validation set split')
    parser.add_argument('--batch-size', type=int, default=32, help='Batch size for training and evaluation')
    parser.add_argument('--num-cross-validation-folds', type=int, default=1, help='')
    # Learning rate
    parser.add_argument('--weight-decay', type=float, default=0.0001, help='Learning rate for training')
    parser.add_argument('--lr', type=float, default=0.001, help='Learning rate for training')
    parser.add_argument('--warmup-epochs', type=int, default=2, help='Number of warmup epochs')
    parser.add_argument('--lr-schedule', type=str, default='constant', choices=['constant', 'linear', 'cosine'], help='Learning rate scheduling strategy')
    # Model architecture
    parser.add_argument('--depth', type=int, default=3, help='Depth of the model. Minimum 2. If 2, hidden_dim must equal the input dim.')
    parser.add_argument('--hidden-dim', type=int, default=512, help='Dimension of hidden layers')
    parser.add_argument('--activation-fn', type=str, default='relu', choices=['relu'], help='Activation function')
    parser.add_argument('--combine-input', type=str, default='concat', choices=['concat', 'none'], help='Method to combine input')
    parser.add_argument('--dropout-rate', type=float, default=0.1, help='Dropout rate')
    # W&B
    parser.add_argument('--wandb-off', action='store_false', help='')
    parser.add_argument('--wandb-entity', type=str, default='ogb-lsc-comp', help='')
    parser.add_argument('--wandb-project', type=str, default='scaling_mol_gnns', help='')  

    args = parser.parse_args()
    print(json.dumps(vars(args), indent=5))


    # Load the id to fingerprint mapping
    i2v = torch.load(args.fingerprints_path)

    dataset = PygGraphPropPredDataset(
        name=args.dataset,
        root="ogb-data",
    )

    split_idx = dataset.get_idx_split()
    benchmark = pd.read_csv(f"ogb-data/{args.dataset.replace('-', '_')}/mapping/mol.csv.gz")

    evaluator = Evaluator(args.dataset)
    metric = evaluator.eval_metric
    assert metric == "rocauc"

    # Determine task type and number of classes
    if dataset.num_tasks == 1:
        args.task_type, args.num_classes = "classification", dataset.num_classes
    else:
        args.task_type, args.num_classes = "multi-class", dataset.num_classes

    _, _, test_dl, args.input_dim, args.num_tasks = dataloader_factory("test", benchmark, split_idx, i2v, args)

    results = {}

    for seed, fold in zip(SEEDS, range(args.num_cross_validation_folds)):
        # Construct dataloaders
        train_dl, val_dl, _, _, _ = dataloader_factory("train+val", benchmark, split_idx, i2v, args, seed=seed)    

        # Define a model
        model, loss_fn, optimizer, args.trainable_params = model_factory(args)

        # Initialize wandb
        run_name = f"{args.model_name}_{args.dataset}"
        mode = 'disabled' if args.wandb_off is False else None
        wandb.init(project=args.wandb_project, entity=args.wandb_entity, name=run_name, mode=mode)
        wandb.config.update(args)

        # Test random model
        epoch = 0
        # evaluate(model, test_dl, loss_fn, args.task_type, evaluation_type='test', epoch=epoch)
        
        best_epoch = {'val_results': None, 'model': None}
        # Training and validation loop
        for epoch in range(args.epochs):
            print(f"## Fold {fold+1}/{args.num_cross_validation_folds} | Epoch {epoch+1}/{args.epochs}")
            adjust_learning_rate(optimizer, epoch, args)
            model = train_one_epoch(model, train_dl, loss_fn, optimizer, args.task_type, epoch, fold)
            val_results = evaluate(model, val_dl, loss_fn, args.task_type, evaluation_type='val', epoch=epoch, fold=fold, evaluator=evaluator)

            # keep best model and validation loss value
            if best_epoch['model'] is None:
                best_epoch['model'] = deepcopy(model)
                best_epoch['val_results'] = deepcopy(val_results)
            else:
                best_epoch['model'] = best_epoch['model'] if best_epoch['val_results']['val_loss'] <= val_results['val_loss'] else deepcopy(model)
                best_epoch['val_results'] = best_epoch['val_results'] if best_epoch['val_results']['val_loss'] <= val_results['val_loss'] else deepcopy(val_results)

        # Test trained model
        eval_results = evaluate(best_epoch['model'], test_dl, loss_fn, args.task_type, evaluation_type='test', epoch=epoch, fold=fold, evaluator=evaluator)
        results = aggregate_dicts(dicts=[results, best_epoch['val_results'], eval_results])

    print(json.dumps(results, indent=5))
    print(json.dumps(calculate_statistics(results), indent=5))
    wandb.run.summary['statistics'] = calculate_statistics(results)
    wandb.finish()

if __name__ == "__main__":
    main()