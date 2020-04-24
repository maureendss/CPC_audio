# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import torch
import math
import concurrent.futures
from . import dtw
import progressbar


def get_distance_function_from_name(name_str):
    if name_str == 'euclidian':
        return get_euclidian_distance_batch
    if name_str == 'cosine':
        return get_cosine_distance_batch
    raise ValueError(f"Invalid distance mode")


def check_dtw_group_validity(a, b, x):
    assert(len(a.size()) == len(b.size()))
    assert(len(a.size()) == len(x.size()))
    assert(a.size(2) == x.size(2))
    assert(a.size(2) == b.size(2))


def get_cosine_distance_batch(a1, a2, epsilon=1e-8):
    r""" a1 and a2 must be normalized"""
    N1, S1, D = a1.size()  # Batch x Seq x Channel
    N2, S2, D = a2.size()  # Batch x Seq x Channel

    prod = (a1.view(N1, 1, S1, 1, D)) * (a2.view(1, N2, 1, S2, D))
    # Sum accross the channel dimension
    prod = torch.clamp(prod.sum(dim=4), -1, 1).acos() / math.pi

    return prod


def get_euclidian_distance_batch(a1, a2):
    N1, S1, D = a1.size()
    N2, S2, D = a2.size()
    diff = a1.view(N1, 1, S1, 1, D) - a2.view(1, N2, 1, S2, D)
    return torch.sqrt((diff**2).sum(dim=4))


def get_distance_group_dtw(a1, a2, size1, size2,
                           ignore_diag=False, symmetric=False,
                           distance_function=get_cosine_distance_batch):

    N1, S1, D = a1.size()
    N2, S2, D = a2.size()
    if size1.size(0) != N1:
        print(a1.size(), size1.size())
        print(a2.size(), size2.size())
    assert(size1.size(0) == N1)
    assert(size2.size(0) == N2)

    distance_mat = distance_function(a1, a2).detach().cpu().numpy()
    return dtw.dtw_batch(a1, a2, size1, size2,
                         distance_mat,
                         ignore_diag, symmetric)


def get_theta_group_dtw(a, b, x, sa, sb, sx, distance_function, symmetric):

    check_dtw_group_validity(a, b, x)

    dxb = get_distance_group_dtw(
        x, b, sx, sb, distance_function=distance_function)
    dxa = get_distance_group_dtw(x, a, sx, sa, ignore_diag=symmetric,
                                 symmetric=symmetric,
                                 distance_function=distance_function)

    Nx, Na = dxa.size()
    Nx, Nb = dxb.size()

    if symmetric:
        n_pos = Na * (Na - 1)
        max_val = dxb.max().item()
        for i in range(Na):
            dxa[i, i] = max_val + 1
    else:
        n_pos = Na * Nx

    dxb = dxb.view(Nx, 1, Nb).expand(Nx, Na, Nb)
    dxa = dxa.view(Nx, Na, 1).expand(Nx, Na, Nb)

    sc = (dxa < dxb).sum() + 0.5 * (dxa == dxb).sum()
    sc /= (n_pos * Nb)

    return sc.item()


def loc_dtw(data, distance_function, symmetric):
    coords, group_a, group_b, group_x = data
    group_a_data, group_a_size = group_a
    group_b_data, group_b_size = group_b
    group_x_data, group_x_size = group_x
    theta = get_theta_group_dtw(group_a_data,
                                group_b_data,
                                group_x_data,
                                group_a_size,
                                group_b_size,
                                group_x_size,
                                distance_function,
                                symmetric)

    return (coords, 1 - theta)


def worker(worker_rank: int, nprocess: int, group_iterator, distance_function, symmetric) -> list:
    bar = progressbar.ProgressBar(maxval=len(group_iterator))
    if worker_rank == 0:
        bar.start()
    worker_data = []
    with torch.no_grad():
        for index, group in enumerate(group_iterator):
            if index % nprocess != worker_rank:
                continue
            if worker_rank == 0:
                bar.update(index)
            worker_data.append((index, loc_dtw(group, distance_function, symmetric)))
    if worker_rank == 0:
        bar.finish()
    if nprocess > 1 and worker_rank == 0:
        print(f'Process {worker_rank} done. Waiting for others to finish...', flush=True)
    return worker_data

def get_abx_scores_dtw_on_group(group_iterator,
                                distance_function,
                                symmetric,
                                nprocess: int = 40):
    data_list = []
    coords_list = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=nprocess) as p:
        jobs = [p.submit(worker, worker_rank, nprocess, group_iterator, distance_function, symmetric) for worker_rank in range(nprocess)]
        all_worker_data = []
        for j in jobs:
            all_worker_data += j.result()
        if nprocess > 1:
            print('All processes done')
        all_worker_data.sort()
        for _, coords_abx in all_worker_data:
            data_list.append(coords_abx[1])
            coords_list.append(coords_abx[0])

    return torch.sparse.FloatTensor(torch.LongTensor(coords_list).t(),
                                    torch.FloatTensor(data_list),
                                    group_iterator.get_board_size())
