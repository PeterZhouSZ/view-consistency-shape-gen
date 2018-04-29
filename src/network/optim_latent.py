import torch
import config
import numpy as np

class RunningAverage():
    def __init__(self):
        self.sum = 0.0
        self.n = 0.0
        self.avg = 0.0

    def reset(self):
        self.sum = 0.0
        self.n = 0.0
        self.avg = 0.0

    def add(self, val, num=1):
        self.sum += float(val)
        self.n += num
        self.avg = float(self.sum) / float(self.n)

    def get(self):
        return self.avg

def fetch_Y(train_src_dataloader):
    Y_list = []
    Y_ind_map = {}
    ind_ctr = 0
    for data in train_src_dataloader:
        for i in xrange(len(data['id'])):
            id_ = str(data['id'][i])
            if id_ not in Y_ind_map:
                t = data['data'][i]
                dim = t.size(0)
                Y_list.append(t.view(1,1,dim,dim,dim))
                Y_ind_map[id_] = ind_ctr
                ind_ctr += 1
    return Y_list, Y_ind_map

def calc_avg(model_ae, model_im, train_target_dataloader, Y_list):

    model_ae.eval()
    model_im.eval()

    # Create tensor version of Y_list
    v_dim = Y_list[0].size(4)
    Y_var_ = torch.Tensor(len(Y_list), 1, v_dim, v_dim, v_dim)
    Y_var_ = torch.cat(Y_list)
    if config.GPU and torch.cuda.is_available():
        Y_var_ = Y_var_.cuda()
    Y_var = torch.autograd.Variable(Y_var_, requires_grad=False).float()

    # Calculate mean
    mean = RunningAverage()
    batch_sample = 999999999999 #TODO: set to proper sample number
    batch_count = 0
    for data in train_target_dataloader:
        if batch_count == batch_sample:
            break

        # Perform forward pass
        ims = data['im']
        if config.GPU and torch.cuda.is_available():
            ims = ims.cuda()
        ims = torch.autograd.Variable(ims).float()
        im_embed = model_im(ims)
        voxel_preds = model_ae.module._decode(im_embed)
        voxel_preds.detach()

        # Find min distance for each img in each batch
        loss_f = torch.nn.BCELoss(reduce=False).cuda()
        for i in xrange(ims.size(0)):
            curr_pred = voxel_preds[i].view(1,1,v_dim,v_dim,v_dim)
            curr_pred_list = [curr_pred for i in xrange(Y_var.size(0))]
            curr_pred_stack = torch.cat(curr_pred_list).cuda()

            l = loss_f(curr_pred_stack, Y_var)
            best_val = float('inf')
            for j in xrange(l.size(0)):
                val = torch.mean(l[j]).item()
                best_val = min(best_val, val)
            mean.add(best_val)
        batch_count += 1
    model_ae.train()
    model_im.train()
    return mean.get()

def calc_rho_density(M, Y_list, Y_ind_map, Y_im_counts, sigma_2):
    
    # Stack Y
    Y_var_ = torch.cat(Y_list).cuda()
    Y_var = torch.autograd.Variable(Y_var_, requires_grad=False).float()

    # Stack M
    v_dim = Y_var.size(4)
    M = M.view(1,1,v_dim,v_dim,v_dim)
    M_list = [M for i in xrange(len(Y_list))]
    M_stack = torch.cat(M_list).cuda()
    M_stack = torch.autograd.Variable(M_stack, requires_grad=False).float()

    # Losses
    total = 0.0
    loss_f = torch.nn.BCELoss(reduce=False).cuda()
    l = loss_f(M_stack, Y_var)
    for y_id in Y_ind_map:
        ind = Y_ind_map[y_id]
        im_count = Y_im_counts[y_id]
        val = torch.exp((-1.0 * torch.mean(l[ind])) / (2*sigma_2))
        total += (float(im_count) * float(val.item()))
    return total

def init_latents(model_ae, model_im, train_target_dataloader, Y_list, Y_ind_map, Y_im_counts, sigma_2):

    model_ae.eval()
    model_im.eval()

    ind_ctr = 0
    M_im_counts = train_target_dataloader.dataset.im_counts
    M_rhos = [-float('inf') for i in xrange(len(M_im_counts))]
    M_list = [0 for i in xrange(len(M_im_counts))]
    M_ind_map = {}

    for data in train_target_dataloader:

        names = data['im_name']
        ims = data['im']
        ims = ims.cuda()
        ims = torch.autograd.Variable(ims).float()
        im_embed = model_im(ims)
        out_voxels = model_ae.module._decode(im_embed)
        v_dim = out_voxels.size(3)

        for i in xrange(out_voxels.size(0)):
            name = names[i]
            id_ = name.split("_")[1]
            if id_ not in M_ind_map:
                M_ind_map[id_] = ind_ctr
                ind_ctr += 1
            ind = M_ind_map[id_]

            prop_M = out_voxels[i].view(1,1,v_dim,v_dim,v_dim)
            rho = calc_rho_density(prop_M, Y_list, Y_ind_map, Y_im_counts, sigma_2)
            if rho >= M_rhos[ind]:
                M_rhos[ind] = rho
                M_list[ind] = prop_M.data[0]

    model_ae.train()
    model_im.train()
    return M_list, M_ind_map

def optimal_label(X, Z_list, Z_ind_map):
    best_id = None
    best_dist = float('inf')
    for id_ in Z_ind_map:
        Z = Z_list[Z_ind_map[id_]]
        dist = (torch.sum((X-Z)**2) / M.data.nelement())
        if dist < best_dist:
            best_id = id_
            best_dist = dist
    return best_id

def update_latents(model_ae, model_im, target_dataloader, M_list, M_ind_map, M_im_counts, Y_list, Y_ind_map, Y_im_counts, lambda_view, lambda_align):
    
    # Optimize each independently, given M
    M_list_opt = [None for i in xrange(len(M_list))]
    N = len(M_list)
    src_cardinality = 0
    for id_ in Y_im_counts:
        src_cardinality += Y_im_counts[id_]

    # Preprocess images in dataloader, assign to 
    output_sum = [None for i in xrange(len(M_list))]
    for data in target_dataloader:
        ims, im_names = data['ims'], data['im_names']
        ims = torch.autograd.Variable(ims.cuda(), requires_grad=False)
        im_embed = model_im(ims)
        out_voxels = model_ae.module._encode(im_embed)

        for i in xrange(len(im_names)):
            im_name = im_names[i]
            id_ = im_name.split("_")[1]
            index = M_ind_map[id_]
            if output_sums[index] == None:
                output_sums[index] = torch.zeros(out_voxels[0].size())
            output_sums[index] += out_voxels[i]

    # Update each latent var independently
    for id_ in M_ind_map:

        # Init
        index = M_ind_map[id_]
        M = M_list[index]
        running_sum = torch.zeros(M.size())
        num = 0

        # Align term one (closest image/label given M_i)
        closest_Y_id = optimal_label(M, Y_list, Y_ind_map)
        running_sum += lambda_align * Y_list[Y_ind_map[closest_Y_id]]
        num += (Y_im_counts[closest_Y_id])

        # Align term two (closest M_i given image/label)
        running_sum += lambda_align * Y_list[Y_ind_map[closest_Y_id]]
        num += 1

        # View term
        running_sum += lambda_view * output_sums[index]
        num += M_im_counts[id_]

        # Save
        M_opt = running_sum / float(num)
        M_list_opt[index] = M_opt.clone()
    return M_list_opt