import numpy as np
from tqdm.auto import tqdm
import itertools

from utils import load_pickle, save_pickle, ReplayBuffer, weights_init, show_mel, show_mel_transfer, to_numpy
from models import Encoder, ResGen, Generator, Discriminator
from hparams import *
import shutil

import torch
device = 'cuda' # torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.cuda.set_device(g)

from matplotlib import pyplot as plt
import librosa
import os

from collections import defaultdict

# Preparing directories
print('Outputting to pool', n)
pooldir = '../pool/' + str(n) 
adir = pooldir + '/a'  # creates training debug folder for B2A
bdir = pooldir + '/b'  # creates training debug folder for A2B

# If folder doesn't exist make it
if not os.path.exists(pooldir): 
    os.mkdir(pooldir)
else: 
    print("Warning: Outputing to an existing experiment pool!", n)
    
if not os.path.exists(adir): 
    os.mkdir(adir)
if not os.path.exists(bdir): 
    os.mkdir(bdir)
    
shutil.copy('hparams.py', pooldir)  # backs up hyperparameters for reference (from hparams.py)

# Checks that number of unpaired duplets are divisible by the batch size
assert max_duplets % batch_size == 0, 'Max sample pairs must be divisible by batch size!' 

# Loading training data
melset_A_128 = load_pickle('../pool/melset_'+A+'_128_cont_wn.pickle') 
melset_B_128 = load_pickle('../pool/melset_'+B+'_128_cont_wn.pickle')
print('Melset A size:', len(melset_A_128), 'Melset B size:', len(melset_B_128))
print('Max duplets:', max_duplets)

# Shuffling melspectrograms
rng = np.random.default_rng()
melset_A_128 = rng.permutation(np.array(melset_A_128))
melset_B_128 = rng.permutation(np.array(melset_B_128))
melset_A_128 = torch.from_numpy(melset_A_128)  # Torch conversion
melset_B_128 = torch.from_numpy(melset_B_128)

# Model Instantiation
enc = Encoder().to(device)  # Shared encoder model
res = ResGen().to(device)  # Shared residual decoding block
dec_A2B = Generator().to(device)  # Generator and Discriminator for Speaker A to B
disc_B = Discriminator(loss_mode).to(device)
dec_B2A = Generator().to(device)  # Generator and Discriminator for Speaker B to A
disc_A = Discriminator(loss_mode).to(device)

# Initialise weights
if curr_epoch == 0:
    enc.apply(weights_init) 
    res.apply(weights_init)  
    dec_A2B.apply(weights_init)
    dec_B2A.apply(weights_init)
    disc_A.apply(weights_init)
    disc_B.apply(weights_init)
else:  # Load previous weights for when curr epochs are more than zero, 
    enc.load_state_dict(torch.load(pooldir+'/enc.pt')) 
    res.load_state_dict(torch.load(pooldir+'/res.pt')) 
    dec_A2B.load_state_dict(torch.load(pooldir+'/dec_A2B.pt'))
    dec_B2A.load_state_dict(torch.load(pooldir+'/dec_B2A.pt'))
    disc_A.load_state_dict(torch.load(pooldir+'/disc_A.pt'))
    disc_B.load_state_dict(torch.load(pooldir+'/disc_B.pt'))    

# Instantiate buffers 
fake_A_buffer = ReplayBuffer() 
fake_B_buffer = ReplayBuffer()

# Initialise optimizers
optim_enc = torch.optim.Adam(enc.parameters(), lr=learning_rate) 
optim_res = torch.optim.Adam(enc.parameters(), lr=learning_rate) 
optim_dec = torch.optim.Adam(itertools.chain(dec_A2B.parameters(), dec_B2A.parameters()),lr=learning_rate)
optim_disc_A = torch.optim.Adam(disc_A.parameters(), lr=learning_rate)
optim_disc_B = torch.optim.Adam(disc_B.parameters(), lr=learning_rate)

train_hist = defaultdict(list)  # Initialise loss history lists

# =====================================================================================================
#                                       Loss functions
# =====================================================================================================

# Initialize criterions
criterion_latent = torch.nn.L1Loss().to(device)
criterion_adversarial = torch.nn.BCELoss().to(device) if (loss_mode=='bce') else torch.nn.MSELoss().to(device)

# Encoder loss function for encoder, motivate mapping of same information from input to output
def loss_encoding(logvar, mu, fake_mel, real_mel):
    kld = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
    recon = criterion_latent(fake_mel, real_mel)
    return ((kld * lambda_kld) + recon * lambda_enc) 

# Cyclic loss for reconstruction through opposing encoder, motivate mapping of information differently to output
def loss_cycle(recon_mel, real_mel):
    recon = criterion_latent(recon_mel, real_mel)
    return (recon * lambda_cycle) 

# Latent loss, L1 distance between centroids of each speaker's distribution
def loss_latent(mean_A, mean_B):
    return criterion_latent(mean_A, mean_B) * lambda_latent

# Adversarial loss function for decoder and discriminator seperately
def loss_adversarial(output, label):
    loss = criterion_adversarial(output, label) * lambda_dec
    return loss

# =====================================================================================================
#                                       The Training Loop
# =====================================================================================================
pbar = tqdm(range(curr_epoch, max_epochs), desc='Epochs')  # init epoch pbar
for i in pbar:
    
    pbar_sub = tqdm(range(0, max_duplets, batch_size),leave=False, desc='Batches')  # init batch pbar
    for j in pbar_sub:
        
        # Loading real samples from each speaker in batches
        real_mel_A = melset_A_128[j : j + batch_size].to(device)
        real_mel_B = melset_B_128[j : j + batch_size].to(device)
        
	    # Testing that loss can firstly go down with same batch
        #real_mel_A = melset_A_128[0 : batch_size].to(device)
        #real_mel_B = melset_B_128[0 : batch_size].to(device)
        
        # Resizing to model tensors
        real_mel_A = real_mel_A.view(batch_size, 1, 128, 128)
        real_mel_B = real_mel_B.view(batch_size, 1, 128, 128)

        # Real data labelled 1, fake data labelled 0
        batch_size = real_mel_A.size(0)
        real_label = torch.squeeze(torch.full((batch_size, 1), 1, device=device, dtype=torch.float32))
        fake_label = torch.squeeze(torch.full((batch_size, 1), 0, device=device, dtype=torch.float32))
        
        # =====================================================
        #                    Generation Pass
        # =====================================================  

        # Forward pass for B to A
        latent_mel_B, mu_B, logvar_B = enc(real_mel_B)
        pseudo_mel_B = res(latent_mel_B)
        fake_mel_A = dec_B2A(pseudo_mel_B)
        fake_output_A = torch.squeeze(disc_A(fake_mel_A))
        
        # Cyclic reconstuction from fake A to B
        latent_recon_A, mu_recon_A, logvar_recon_A = enc(fake_mel_A)
        pseudo_recon_A = res(latent_recon_A)
        recon_mel_B = dec_A2B(pseudo_recon_A)  
        
        # Forward pass for A to B
        latent_mel_A, mu_A, logvar_A = enc(real_mel_A)
        pseudo_mel_A = res(latent_mel_A)
        fake_mel_B = dec_A2B(pseudo_mel_A)
        fake_output_B = torch.squeeze(disc_B(fake_mel_B))
        
        # Cyclic reconstuction from fake B to A
        latent_recon_B, mu_recon_B, logvar_recon_B = enc(fake_mel_B)
        pseudo_recon_B = res(latent_recon_B)
        recon_mel_A = dec_B2A(pseudo_recon_B)  
        
        # =====================================================
        #                  Plotting inference
        # ===================================================== 
        
        # Save generator B2A output per epoch
        d_in, d_recon, d_out, d_target = to_numpy(real_mel_B), to_numpy(recon_mel_B), to_numpy(fake_mel_A), to_numpy(real_mel_A)
        show_mel_transfer(i, d_in, d_recon, d_out, d_target, pooldir + '/a/a_fake_epoch_'+ str(i) + '.png')

        # Save generator A2B output per epoch
        d_in, d_recon, d_out, d_target = to_numpy(real_mel_A), to_numpy(recon_mel_A), to_numpy(fake_mel_B), to_numpy(real_mel_B)
        show_mel_transfer(i, d_in, d_recon, d_out, d_target, pooldir + '/b/b_fake_epoch_'+ str(i) + '.png')
        
        # =====================================================
        #            Generator Loss computation
        # ===================================================== 
        
        # Encoding loss A and B
        loss_enc_A = loss_encoding(logvar_A, mu_A, fake_mel_B, real_mel_A)
        loss_enc_B = loss_encoding(logvar_B, mu_B, fake_mel_A, real_mel_B)
        
        # Decoder/Generator loss
        loss_dec_B2A = loss_adversarial(fake_output_A, real_label)
        loss_dec_A2B = loss_adversarial(fake_output_B, real_label)
        
        # Cyclic loss
        loss_cycle_ABA = loss_cycle(recon_mel_A, real_mel_A)
        loss_cycle_BAB = loss_cycle(recon_mel_B, real_mel_B)

        # Latent loss
        loss_lat = loss_latent(mu_A, mu_B)
        
        # Resetting gradients
        optim_enc.zero_grad()
        optim_res.zero_grad()  
        optim_dec.zero_grad() 
        
        # Backward pass for encoder and update all res/dec generator components
        errDec = loss_dec_A2B + loss_dec_B2A + loss_cycle_ABA + loss_cycle_BAB + loss_enc_B + loss_enc_A + loss_lat 
        errDec.backward()
        optim_enc.step()
        optim_res.step()
        optim_dec.step()
        
        # =====================================================
        #                   Discriminators update
        # =====================================================

        # Forward pass disc_A
        real_out_A = torch.squeeze(disc_A(real_mel_A))
        fake_mel_A = fake_A_buffer.push_and_pop(fake_mel_A)
        fake_out_A = torch.squeeze(disc_A(fake_mel_A.detach()))
        
        loss_D_real_A = loss_adversarial(real_out_A, real_label)
        loss_D_fake_A = loss_adversarial(fake_out_A, fake_label)
        errDisc_A = (loss_D_real_A + loss_D_fake_A) / 2
        
        # Forward pass disc_B
        real_out_B = torch.squeeze(disc_B(real_mel_B))
        fake_mel_B = fake_B_buffer.push_and_pop(fake_mel_B)
        fake_out_B = torch.squeeze(disc_B(fake_mel_B.detach()))

        loss_D_real_B = loss_adversarial(real_out_B, real_label)
        loss_D_fake_B = loss_adversarial(fake_out_B, fake_label)
        errDisc_B = (loss_D_real_B + loss_D_fake_B) / 2
                
        # Resetting gradients
        optim_disc_A.zero_grad()
        optim_disc_B.zero_grad()
        
        # Backward pass and update all
        errDisc_A.backward()
        errDisc_B.backward()
        optim_disc_A.step()
        optim_disc_B.step() 
        
        # Update error log
        pbar.set_postfix(vA=loss_enc_A.item(),vB=loss_enc_B.item(), A2B=loss_dec_A2B.item(), B2A=loss_dec_B2A.item(), 
        ABA=loss_cycle_ABA.item(), BAB=loss_cycle_BAB.item(), disc_A=errDisc_A.item(), disc_B=errDisc_B.item())
        
        # Update error history every batch update 
        train_hist['enc_A'].append(loss_enc_A.item())
        train_hist['enc_B'].append(loss_enc_B.item())
        train_hist['enc_lat'].append(loss_lat.item())
        train_hist['dec_B2A'].append(loss_dec_B2A.item())
        train_hist['dec_A2B'].append(loss_dec_A2B.item())
        train_hist['dec_ABA'].append(loss_cycle_ABA.item())
        train_hist['dec_BAB'].append(loss_cycle_BAB.item())
        train_hist['dec'].append(errDec.item())
        train_hist['disc_A'].append(errDisc_A.item())
        train_hist['disc_B'].append(errDisc_B.item())
    
    # Saving every 10 epochs (or on last epoch)
    if(i % 10 == 0 or i == 99):
        # Save updated training history and model weights
        save_pickle(train_hist, pooldir +'/train_hist.pickle')
        torch.save(dec_A2B.state_dict(),  pooldir +'/dec_A2B.pt')
        torch.save(dec_B2A.state_dict(),  pooldir +'/dec_B2A.pt')
        torch.save(res.state_dict(), pooldir +'/res.pt')
        torch.save(enc.state_dict(), pooldir +'/enc.pt')
        torch.save(disc_A.state_dict(),  pooldir +'/disc_A.pt')
        torch.save(disc_B.state_dict(),  pooldir +'/disc_B.pt')
        
        