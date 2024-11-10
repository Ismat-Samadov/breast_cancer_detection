# Install required dependencies
!pip install -U pydicom
!pip install -U gdcm
!pip install -U pylibjpeg pylibjpeg-libjpeg>=2.1
!pip install -U jpg2dcm

# Set environment variable to disable albumentations update check
import os
os.environ['NO_ALBUMENTATIONS_UPDATE'] = '1'

# Imports
import numpy as np
import pandas as pd
import pydicom
pydicom.config.GDCM_HANDLER = True  # Use GDCM for DICOM reading
import cv2
from pathlib import Path
from sklearn.model_selection import GroupKFold
import albumentations as A
from albumentations.pytorch import ToTensorV2
from tqdm.auto import tqdm
import matplotlib.pyplot as plt
import seaborn as sns
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torch.cuda.amp import autocast, GradScaler
import timm
from sklearn.metrics import roc_auc_score
import gc
import warnings
warnings.filterwarnings('ignore')

class CFG:
    """Configuration class containing all parameters"""
    # Debug mode
    debug = True  # Set to False for full training
    
    # Paths
    base_path = Path("/kaggle/input/rsna-breast-cancer-detection")
    processed_dir = Path("/kaggle/working/processed_images")
    model_dir = Path("/kaggle/working/models")
    
    # Preprocessing
    target_size = (512, 512)  # Reduced from 2048 for memory efficiency
    output_format = 'png'
    
    # Training parameters
    seed = 42
    epochs = 2 if debug else 10
    train_batch_size = 16
    valid_batch_size = 32
    num_workers = 4
    num_folds = 5
    
    # Model
    model_name = 'efficientnet_b3'
    pretrained = True
    target_size = 1
    
    # Optimizer
    optimizer = 'AdamW'
    learning_rate = 1e-4
    weight_decay = 1e-6
    
    # Scheduler
    scheduler = 'CosineAnnealingLR'
    min_lr = 1e-7
    T_max = int(epochs * 0.7)
    
    # Augmentations
    train_aug_list = [
        A.RandomRotate90(p=0.5),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.ShiftScaleRotate(p=0.5),
        A.OneOf([
            A.GaussNoise(var_limit=[10, 50]),
            A.GaussianBlur(),
            A.MotionBlur(),
        ], p=0.3),
        A.GridDistortion(p=0.3),
        A.CoarseDropout(max_holes=8, max_width=20, max_height=20, p=0.3),
        A.Normalize(),
        ToTensorV2(),
    ]
    
    valid_aug_list = [
        A.Normalize(),
        ToTensorV2(),
    ]
    
            
class RSNAPreprocessor:
    """Handles preprocessing of DICOM images"""
    def __init__(self, **kwargs):
        self.base_path = kwargs.get('base_path', CFG.base_path)
        self.target_size = kwargs.get('target_size', CFG.target_size)
        self.output_format = kwargs.get('output_format', CFG.output_format)
        
        self.train_images_path = self.base_path / "train_images"
        self.test_images_path = self.base_path / "test_images"
        
        if self.output_format not in ['png', 'jpg', 'jpeg']:
            raise ValueError("output_format must be 'png' or 'jpg'/'jpeg'")

    def read_dicom(self, patient_id, image_id, is_train=True):
        try:
            images_path = self.train_images_path if is_train else self.test_images_path
            dicom_path = images_path / str(patient_id) / f"{image_id}.dcm"
            
            if not dicom_path.exists():
                print(f"File not found: {dicom_path}")
                return None
            
            # Configure pydicom to use GDCM
            pydicom.config.image_handlers = [None]  # Reset handlers
            if pydicom.have_gdcm():
                pydicom.config.image_handlers = [pydicom.pixel_data_handlers.gdcm_handler]
            
            # Read DICOM with specific transfer syntax handling
            try:
                dicom = pydicom.dcmread(str(dicom_path), force=True)
                
                # Check if image needs decompression
                if hasattr(dicom, 'file_meta') and hasattr(dicom.file_meta, 'TransferSyntaxUID'):
                    if dicom.file_meta.TransferSyntaxUID.is_compressed:
                        # Try GDCM decompression
                        try:
                            dicom.decompress()
                        except Exception as e:
                            print(f"GDCM decompression failed, trying alternative method: {str(e)}")
                            # Alternative method using pylibjpeg if GDCM fails
                            pydicom.config.image_handlers = [pydicom.pixel_data_handlers.pillow_handler]
                            dicom = pydicom.dcmread(str(dicom_path), force=True)
                
                img = self._process_dicom_image(dicom)
                return img
                
            except Exception as e:
                print(f"Error reading DICOM: {str(e)}")
                return None

        except Exception as e:
            print(f"Error processing image {image_id} for patient {patient_id}: {str(e)}")
            return None

    def _try_alternate_reading(self, dicom_path):
        """Try different methods to read problematic DICOM files"""
        try:
            # Try GDCM first
            pydicom.config.image_handlers = [pydicom.pixel_data_handlers.gdcm_handler]
            dicom = pydicom.dcmread(str(dicom_path), force=True)
            return dicom
        except:
            try:
                # Try PyLibJPEG
                pydicom.config.image_handlers = [pydicom.pixel_data_handlers.pillow_handler]
                dicom = pydicom.dcmread(str(dicom_path), force=True)
                return dicom
            except:
                try:
                    # Try without any specific handler
                    pydicom.config.image_handlers = [None]
                    dicom = pydicom.dcmread(str(dicom_path), force=True)
                    return dicom
                except:
                    return None
       
    def _process_dicom_image(self, dicom):
        try:
            # Convert pixel_array to float32 to avoid precision issues
            img = dicom.pixel_array.astype(np.float32)
            
            # Check if we need to apply VOI LUT transformation
            if hasattr(dicom, 'WindowCenter') and hasattr(dicom, 'WindowWidth'):
                voi_center = dicom.WindowCenter
                voi_width = dicom.WindowWidth
                if isinstance(voi_center, pydicom.multival.MultiValue):
                    voi_center = float(voi_center[0])
                if isinstance(voi_width, pydicom.multival.MultiValue):
                    voi_width = float(voi_width[0])
                    
                voi_lower = voi_center - voi_width / 2
                voi_upper = voi_center + voi_width / 2
                img = np.clip(img, voi_lower, voi_upper)
            
            # Normalize to [0,1]
            if img.max() != img.min():
                img = (img - img.min()) / (img.max() - img.min())
            
            # Convert to uint8 [0,255]
            img = (img * 255).astype(np.uint8)
            
            # Apply CLAHE for better contrast
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            img = clahe.apply(img)
            
            # Handle different photometric interpretations
            if hasattr(dicom, 'PhotometricInterpretation'):
                if dicom.PhotometricInterpretation == "MONOCHROME1":
                    img = 255 - img
            
            return img
            
        except Exception as e:
            print(f"Error in _process_dicom_image: {str(e)}")
            return None

    def _resize_with_padding(self, img):
        if img is None:
            return None
            
        aspect = img.shape[0] / img.shape[1]
        if aspect > 1:
            new_height = self.target_size[0]
            new_width = int(new_height / aspect)
        else:
            new_width = self.target_size[1]
            new_height = int(new_width * aspect)
        
        img = cv2.resize(img, (new_width, new_height))
        
        # Add padding
        top_pad = (self.target_size[0] - img.shape[0]) // 2
        bottom_pad = self.target_size[0] - img.shape[0] - top_pad
        left_pad = (self.target_size[1] - img.shape[1]) // 2
        right_pad = self.target_size[1] - img.shape[1] - left_pad
        
        return cv2.copyMakeBorder(
            img, top_pad, bottom_pad, left_pad, right_pad,
            cv2.BORDER_CONSTANT, value=0
        )

    def save_image(self, img, output_path):
        if img is not None:
            if self.output_format == 'png':
                cv2.imwrite(str(output_path.with_suffix('.png')), img)
            else:
                cv2.imwrite(str(output_path.with_suffix('.jpg')), img, [cv2.IMWRITE_JPEG_QUALITY, 100])

    def process_and_save(self, metadata_df, output_dir, num_samples=None):
        if num_samples:
            metadata_df = metadata_df.head(num_samples)
        
        output_dir = Path(output_dir)
        self._create_directory_structure(output_dir)
        
        processed_count = 0
        failed_count = 0
        
        for idx, row in tqdm(metadata_df.iterrows(), total=len(metadata_df)):
            try:
                img = self.read_dicom(
                    patient_id=str(row['patient_id']),
                    image_id=str(row['image_id'])
                )
                
                if img is not None:
                    # Save main image
                    output_path = output_dir / row['view'] / row['laterality'] / f"{row['patient_id']}_{row['image_id']}"
                    self.save_image(img, output_path)
                    
                    # Save thumbnail
                    thumbnail = cv2.resize(img, (512, 512))
                    thumbnail_path = output_path.with_name(f"{output_path.stem}_thumb")
                    self.save_image(thumbnail, thumbnail_path)
                    
                    processed_count += 1
                else:
                    failed_count += 1
                    
            except Exception as e:
                failed_count += 1
                print(f"Error processing row {idx}: {str(e)}")
        
        return processed_count, failed_count

    def _create_directory_structure(self, output_dir):
        output_dir.mkdir(exist_ok=True)
        for view in ['CC', 'MLO']:
            (output_dir / view).mkdir(exist_ok=True)
            (output_dir / view / 'L').mkdir(exist_ok=True)
            (output_dir / view / 'R').mkdir(exist_ok=True)

class RSNADataset(Dataset):
    """PyTorch Dataset for RSNA images"""
    def __init__(self, df, transform=None, is_train=True):
        self.df = df
        self.transform = transform
        self.is_train = is_train
        
    def __len__(self):
        return len(self.df)
    
    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        img_path = CFG.processed_dir / row['view'] / row['laterality'] / f"{row['patient_id']}_{row['image_id']}.png"
        
        img = cv2.imread(str(img_path))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        
        if self.transform:
            img = self.transform(image=img)['image']
            
        if self.is_train:
            label = torch.tensor(row['cancer'], dtype=torch.float32)
            return img, label
        else:
            return img

class RSNAModel(nn.Module):
    """Model architecture"""
    def __init__(self):
        super().__init__()
        self.model = timm.create_model(
            CFG.model_name, 
            pretrained=CFG.pretrained, 
            num_classes=CFG.target_size
        )
        
    def forward(self, x):
        return self.model(x)

class AverageMeter:
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()
        
    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        
    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def train_one_epoch(model, train_loader, criterion, optimizer, scheduler, device):
    """Trains the model for one epoch"""
    model.train()
    scaler = GradScaler()
    losses = AverageMeter()
    
    pbar = tqdm(train_loader, desc='Training')
    
    for images, labels in pbar:
        images = images.to(device)
        labels = labels.to(device)
        
        with autocast():
            y_preds = model(images).squeeze(1)
            loss = criterion(y_preds, labels)
        
        losses.update(loss.item(), labels.size(0))
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad()
        
        if scheduler is not None:
            scheduler.step()
            
        pbar.set_postfix({'train_loss': losses.avg})
    
    return losses.avg

def valid_one_epoch(model, valid_loader, criterion, device):
    """Validates the model for one epoch"""
    model.eval()
    losses = AverageMeter()
    preds = []
    targets = []
    
    pbar = tqdm(valid_loader, desc='Validation')
    
    with torch.no_grad():
        for images, labels in pbar:
            images = images.to(device)
            labels = labels.to(device)
            
            y_preds = model(images).squeeze(1)
            loss = criterion(y_preds, labels)
            
            losses.update(loss.item(), labels.size(0))
            preds.append(y_preds.sigmoid().cpu().numpy())
            targets.append(labels.cpu().numpy())
            
            pbar.set_postfix({'valid_loss': losses.avg})
    
    preds = np.concatenate(preds)
    targets = np.concatenate(targets)
    score = roc_auc_score(targets, preds)
    
    return losses.avg, score

def train_model():
    """Main training loop"""
    # Set random seeds
    torch.manual_seed(CFG.seed)
    np.random.seed(CFG.seed)
    
    # Read and prepare data
    train_df = pd.read_csv(CFG.base_path / 'train.csv')
    if CFG.debug:
        train_df = train_df.head(100)
    
    # Create folds
    kfold = GroupKFold(n_splits=CFG.num_folds)
    for fold, (train_idx, val_idx) in enumerate(kfold.split(train_df, groups=train_df['patient_id'])):
        train_df.loc[val_idx, 'fold'] = fold
    
    # Training loop
    for fold in range(CFG.num_folds):
        print(f'Training fold {fold + 1}/{CFG.num_folds}')
        
        # Prepare data
        train_loader = DataLoader(
            RSNADataset(
                train_df[train_df.fold != fold],
                transform=A.Compose(CFG.train_aug_list)
            ),
            batch_size=CFG.train_batch_size,
            shuffle=True,
            num_workers=CFG.num_workers,
            pin_memory=True
        )
        
        valid_loader = DataLoader(
            RSNADataset(
                train_df[train_df.fold == fold],
                transform=A.Compose(CFG.valid_aug_list)
            ),
            batch_size=CFG.valid_batch_size,
            shuffle=False,
            num_workers=CFG.num_workers,
            pin_memory=True
        )
        
        # Initialize model and training
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        model = RSNAModel().to(device)
        criterion = nn.BCEWithLogitsLoss()
        optimizer = getattr(torch.optim, CFG.optimizer)(
            model.parameters(),
            lr=CFG.learning_rate,
            weight_decay=CFG.weight_decay
        )
        scheduler = getattr(torch.optim.lr_scheduler, CFG.scheduler)(
            optimizer,
            T_max=CFG.T_max,
            eta_min=CFG.min_lr
        )
        
        # Training
        best_score = 0
        for epoch in range(CFG.epochs):
            print(f'Epoch {epoch + 1}/{CFG.epochs}')
            
            train_loss = train_one_epoch(
                model, train_loader, criterion,
                optimizer, scheduler, device
            )
            
            valid_loss, valid_score = valid_one_epoch(
                model, valid_loader, criterion, device
            )
            
            print(f'Train Loss: {train_loss:.4f} Valid Loss: {valid_loss:.4f} Valid Score: {valid_score:.4f}')
            
            if valid_score > best_score:
                best_score = valid_score
                torch.save(model.state_dict(), CFG.model_dir / f'fold{fold}_best.pth')
                print(f'Best model saved! Score: {best_score:.4f}')
        
        # Cleanup
        del model, train_loader, valid_loader
        gc.collect()
        torch.cuda.empty_cache()

def inference():
    """Performs inference using trained models"""
    print("\nStarting inference...")
    
    # Read test data
    test_df = pd.read_csv(CFG.base_path / 'test.csv')
    if CFG.debug:
        test_df = test_df.head(100)
        print("Debug mode: Using only 100 test samples")
    
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    predictions = []
    
    # Create test dataset
    test_dataset = RSNADataset(
        test_df,
        transform=A.Compose(CFG.valid_aug_list),
        is_train=False
    )
    test_loader = DataLoader(
        test_dataset,
        batch_size=CFG.valid_batch_size,
        shuffle=False,
        num_workers=CFG.num_workers,
        pin_memory=True
    )
    
    # Inference with all folds
    for fold in range(CFG.num_folds):
        print(f'Inferencing fold {fold + 1}/{CFG.num_folds}')
        model = RSNAModel().to(device)
        
        try:
            model.load_state_dict(torch.load(CFG.model_dir / f'fold{fold}_best.pth'))
            model.eval()
            
            fold_preds = []
            with torch.no_grad():
                for images in tqdm(test_loader, desc=f'Fold {fold + 1}'):
                    images = images.to(device)
                    y_preds = model(images).squeeze(1)
                    fold_preds.append(y_preds.sigmoid().cpu().numpy())
            
            fold_preds = np.concatenate(fold_preds)
            predictions.append(fold_preds)
            
        except Exception as e:
            print(f"Error in fold {fold} inference: {str(e)}")
            continue
        
        finally:
            del model
            gc.collect()
            torch.cuda.empty_cache()
    
    # Average predictions from all folds
    predictions = np.mean(predictions, axis=0)
    
    # Create submission
    submission = pd.DataFrame({
        'prediction_id': test_df['prediction_id'],
        'cancer': predictions
    })
    submission.to_csv('submission.csv', index=False)
    print('Submission saved!')
    return submission

def process_test_data():
    """Processes test data for inference"""
    print("\nProcessing test data...")
    test_df = pd.read_csv(CFG.base_path / 'test.csv')
    if CFG.debug:
        test_df = test_df.head(100)
    
    preprocessor = RSNAPreprocessor(
        base_path=CFG.base_path,
        target_size=CFG.target_size,
        output_format=CFG.output_format
    )
    
    processed_count, failed_count = preprocessor.process_and_save(
        test_df,
        CFG.processed_dir,
        num_samples=None if not CFG.debug else 100
    )
    print(f"Test data processing completed. Processed: {processed_count}, Failed: {failed_count}")

def main():
    """Main execution function"""
    print("Starting RSNA Mammography Pipeline...")
    
    try:
        # Create necessary directories
        CFG.processed_dir.mkdir(parents=True, exist_ok=True)
        CFG.model_dir.mkdir(parents=True, exist_ok=True)
        
        # Step 1: Process training data
        print("\nStep 1: Processing training data...")
        train_df = pd.read_csv(CFG.base_path / 'train.csv')
        if CFG.debug:
            train_df = train_df.head(100)
            print("Debug mode: Using only 100 training samples")
        
        preprocessor = RSNAPreprocessor(
            base_path=CFG.base_path,
            target_size=CFG.target_size,
            output_format=CFG.output_format
        )
        
        processed_count, failed_count = preprocessor.process_and_save(
            train_df,
            CFG.processed_dir,
            num_samples=None if not CFG.debug else 100
        )
        print(f"Training data processing completed. Processed: {processed_count}, Failed: {failed_count}")
        
        # Step 2: Train models
        print("\nStep 2: Training models...")
        train_model()
        
        # Step 3: Process test data
        print("\nStep 3: Processing test data...")
        process_test_data()
        
        # Step 4: Generate predictions
        print("\nStep 4: Generating predictions...")
        submission = inference()
        
        print("\nPipeline completed successfully!")
        
        if CFG.debug:
            print("\nNote: This was run in debug mode. Set CFG.debug = False for full training.")
        
        return submission
        
    except Exception as e:
        print(f"\nAn error occurred: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return None

if __name__ == "__main__":
    main()