import torch
from trainers.utils.diff_ops import gradient, jacobian


def outter(v1, v2):
    """
    Batched outter product of two vectors: [v1] [v2]^T
    :param v1: (bs, dim)
    :param v2: (bs, dim)
    :return: (bs, dim, dim)
    """
    bs = v1.size(0)
    d = v1.size(1)
    v1 = v1.view(bs, d, 1)
    v2 = v2.view(bs, 1, d)
    return torch.bmm(v1, v2)


def _addr_(mat, vec1, vec2, alpha=1., beta=1.):
    """
    Return
        alpha * outter(vec1, vec2) + beta * [mat]
    :param mat:  (bs, npoints, dim, dim)
    :param vec1: (bs, npoints, dim)
    :param vec2: (bs, npoints, dim)
    :param alpha: float
    :param beta: float
    :return:
    """
    bs, npoints, dim =vec1.size(0), vec1.size(1), vec1.size(2)
    assert len(mat.size()) == 4
    outter_n = outter(vec1.view(bs * npoints, dim), vec2.view(bs * npoints, dim))
    outter_n = outter_n.view(bs, npoints, dim, dim)
    out = alpha * outter_n + beta * mat.view(bs, npoints, dim, dim)
    return out


def get_surf_pcl(net, npoints=1000, dim=3, steps=5, eps=1e-4,
                 noise_sigma=0.01, filtered=True, sigma_decay=1.,
                 max_repeat=10, bound=(1 - 1e-4),
                 use_rejection=False, rejection_bs=100000, rejection_thr=0.05):
    if use_rejection:
        return get_surf_pcl_with_rejection(
            net, npoints=npoints, batch_size=rejection_bs, dim=dim,
            thr=rejection_thr, gstep=True)
    else:
        return get_surf_pcl_defaut(
            net, npoints=npoints, dim=dim, steps=steps, eps=eps,
            noise_sigma=noise_sigma, filtered=filtered, sigma_decay=sigma_decay,
            max_repeat=max_repeat, bound=bound)


def get_surf_pcl_with_rejection(
        net, npoints=1000, batch_size=100000, dim=3, thr=0.05, gstep=True):
    out = []
    out_cnt = 0
    with torch.no_grad():
        while out_cnt < npoints:
            x = torch.rand(1, batch_size, dim).cuda().float() * 2 - 1
            y = torch.abs(net(x))
            m = (y < thr).view(1, batch_size)
            m_cnt = m.sum().detach().cpu().item()
            if m_cnt < 1:
                continue
            x_eq = x[m].view(m_cnt, dim)
            out.append(x_eq)
            out_cnt += m_cnt
    x = torch.cat(out, dim=0)[:npoints, :]
    if gstep:
        if x.is_leaf:
            x.requires_grad = True
        else:
            x.retain_grad()
        y = net(x)
        g = gradient(y, x).view(npoints, dim).detach()
        g = g / g.norm(dim=-1, keepdim=True)
        x = x - g * y
    return x


def get_surf_pcl_defaut(
        net, npoints=1000, dim=3, steps=5, eps=1e-4,
        noise_sigma=0.01, filtered=True, sigma_decay=1.,
        max_repeat=10, bound=(1 - 1e-4)):
    out_cnt = 0
    out = None
    already_repeated = 0
    while out_cnt < npoints and already_repeated < max_repeat:
        already_repeated += 1
        x = torch.rand(npoints, dim).cuda().float() * 2 - 1
        for i in range(steps):
            sigma_i = noise_sigma * sigma_decay ** i
            x = x.detach() + torch.randn_like(x).to(x) * sigma_i
            x.requires_grad = True
            y = net(x)
            if torch.allclose(y, torch.zeros_like(y)):
                break

            g = gradient(y, x).view(npoints, dim).detach()
            g = g / (g.norm(dim=-1, keepdim=True) + eps)
            x = torch.clamp(x - g * y, min=-bound, max=bound)

        if filtered:
            with torch.no_grad():
                y = net(x)
                mask = (torch.abs(y) < eps).view(-1, 1)
                x = x.view(-1, dim).masked_select(mask).view(-1, dim)
                out_cnt += x.shape[0]
                if out is None:
                    out = x
                else:
                    out = torch.cat([x, out], dim=0)
        else:
            out = x
            out_cnt = npoints
    out = out[:npoints, :]
    return out


def tangential_projection_matrix(y, x):
    bs, npoints, dim = x.size(0), x.size(1), x.size(2)
    grad = gradient(y, x)
    normals = (grad / grad.norm(dim=-1, keepdim=True)).view(bs, npoints, dim)
    normals_proj = _addr_(
        torch.eye(dim).view(1, 1, dim, dim).expand(bs, npoints, -1, -1).to(y),
        normals, normals, alpha=-1
    )
    return normals, normals_proj


def compute_deform_weight(
        x, deform, y_net, x_net, surface=False, detach=True, normalize=True):
    """

    :param x:
    :param deform:
    :param y_net:
    :param x_net:
    :param surface:
    :param detach:
    :param normalize:
    :return:
    """
    bs, npoints, dim = x.size(0), x.size(1), x.size(2)
    x = x.clone().detach()
    x.requires_grad = True
    y = deform(x).view(bs, npoints, dim)
    J, status = jacobian(y, x)
    assert status == 0

    if surface:
        # Find the change of area along the tangential plane
        yn, yn_proj = tangential_projection_matrix(y_net(y), y)
        xn, xn_proj = tangential_projection_matrix(x_net(x), x)

        J = torch.bmm(
            J.view(-1, dim, dim),
            xn_proj.view(-1, dim, dim)
        )
        J = _addr_(J.view(bs, npoints, dim, dim),
                   yn.view(bs, npoints, dim),
                   xn.view(bs, npoints, dim))

    weight = torch.abs(torch.linalg.det(J.view(bs * npoints, dim, dim)))
    if int(dim) == 3:
        weight = weight ** 2
    weight = 1. / weight.view(bs, npoints)

    if normalize:
        weight = weight / weight.sum(dim=-1, keepdim=True) * npoints

    if detach:
        weight = weight.detach()
    return weight


def sample_points_for_loss(
        npoints, dim=3, use_surf_points=False, gtr=None, net=None,
        deform=None, invert_sampling=False, return_weight=False,
        detach_weight=True, use_rejection=False):
    if use_surf_points:
        if invert_sampling:
            assert deform is not None
            assert gtr is not None
            y = get_surf_pcl(
                lambda x: gtr(x), npoints=npoints, dim=dim,
                steps=5, noise_sigma=1e-3, filtered=False,
                sigma_decay=1., use_rejection=use_rejection
            )
            x = deform.invert(y, iters=30).detach().cuda().float()

            weight = compute_deform_weight(
                x.view(1, -1, dim),
                lambda x: deform(x, None),
                y_net=lambda x: gtr(x),
                x_net=lambda x: net(x)[0],
                surface=True, detach=detach_weight)
        else:
            assert net is not None
            x = get_surf_pcl(
                lambda x: net(x)[0], npoints=npoints, dim=dim,
                steps=5, noise_sigma=1e-3, filtered=False, sigma_decay=1.,
                use_rejection=use_rejection
            ).detach().cuda().float()
            weight = torch.ones(1, npoints).cuda().float()
    else:
        x = torch.rand(1, npoints, dim).cuda().float() * 2 - 1
        weight = torch.ones(1, npoints).cuda().float()
        if invert_sampling:
            assert deform is not None
            y = x
            x = deform.invert(y, iters=30).detach().cuda().float()
            weight = compute_deform_weight(
                x.view(1, -1, dim),
                lambda x: deform(x, None),
                y_net=lambda x: gtr(x),
                x_net=lambda x: net(x)[0],
                surface=False, detach=detach_weight)
    bs = 1
    x = x.view(bs, npoints, dim)
    weight = weight.view(bs, npoints)
    if return_weight:
        return x, weight
    else:
        return x