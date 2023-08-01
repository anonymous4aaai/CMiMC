import argparse

import torch
import torch.optim as optim
from torch.utils.data import DataLoader
from tqdm import tqdm
from collections import Counter

from coperception_origin.datasets import V2XSimDet
from coperception_origin.configs import Config, ConfigGlobal

# from coperception_origin.utils.CoDetModule import *
# from coperception_origin.utils.CoDetModule_globalConv import *
from coperception_origin.utils.CoDetModule_DIM import *

# from coperception_origin.utils.MMI import DeepMILoss
# from coperception_origin.utils.MMI_prior import DeepMILoss
# from coperception_origin.utils.MMI_globalConv import DeepMILoss
from coperception_origin.utils.MMI_DIM import DeepMILoss
# from coperception_origin.utils.MMI_DIM_prior import DeepMILoss

from coperception_origin.utils.loss import *
from coperception_origin.models.det import *
from coperception_origin.utils import AverageMeter
from coperception_origin.utils.data_util import apply_pose_noise

import glob
import os


def check_folder(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
    return folder_path


def main(args):
    config = Config("train", binary=True, only_det=True)
    config_global = ConfigGlobal("train", binary=True, only_det=True)

    num_epochs = args.nepoch
    need_log = args.log
    num_workers = args.nworker
    start_epoch = 1
    batch_size = args.batch_size
    compress_level = args.compress_level
    auto_resume_path = args.auto_resume_path
    pose_noise = args.pose_noise
    only_v2i = args.only_v2i
    MMI_flag = args.MMI_flag
    flag_GPU = args.flag_GPU

    # Specify gpu device
    if flag_GPU == 0:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu") # GPU 0
    else:
        device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu") # GPU 1
    device_num = torch.cuda.device_count()
    print("device number", device_num)

    if args.com == "upperbound":
        flag = "upperbound"
    elif args.com == "when2com" and args.warp_flag:
        flag = "when2com_warp"
    elif args.com in [
        "lowerbound",
        "v2v",
        "disco",
        "sum",
        "mean",
        "max",
        "cat",
        "agent",
        "when2com",
    ]:
        flag = args.com
    else:
        raise ValueError(f"com: {args.com} is not supported")

    config.flag = flag

    # num_agent = args.num_agent
    num_agent = 5 if "v1" in args.logpath else 6
    # agent0 is the RSU
    agent_idx_range = range(num_agent) if args.rsu else range(1, num_agent)
    training_dataset = V2XSimDet(
        dataset_roots=[f"{args.data}/agent{i}" for i in agent_idx_range],
        config=config,
        config_global=config_global,
        split="train",
        bound="upperbound" if args.com == "upperbound" else "lowerbound",
        kd_flag=args.kd_flag,
        rsu=args.rsu,
    )
    training_data_loader = DataLoader(
        training_dataset, shuffle=True, batch_size=batch_size, num_workers=num_workers
    )
    print("Training dataset size:", len(training_dataset))

    logger_root = args.logpath if args.logpath != "" else "logs"

    if not args.rsu:
        num_agent -= 1

    if flag == "lowerbound" or flag == "upperbound":
        model = FaFNet(
            config,
            layer=args.layer,
            kd_flag=args.kd_flag,
            num_agent=num_agent,
            compress_level=compress_level,
        )
    elif flag == "when2com" or flag == "when2com_warp":
        model = When2com(
            config,
            layer=args.layer,
            warp_flag=args.warp_flag,
            num_agent=num_agent,
            compress_level=compress_level,
            only_v2i=only_v2i,
        )
    elif flag == "v2v":
        model = V2VNet(
            config,
            gnn_iter_times=args.gnn_iter_times,
            layer=args.layer,
            layer_channel=256,
            num_agent=num_agent,
            compress_level=compress_level,
            only_v2i=only_v2i,
        )
    elif flag == "disco":
        model = DiscoNet(
            config,
            layer=args.layer,
            kd_flag=args.kd_flag,
            num_agent=num_agent,
            compress_level=compress_level,
            only_v2i=only_v2i,
            MMI_flag=MMI_flag
        )


    if flag_GPU == 0:
        model = nn.DataParallel(model,device_ids=[0])
    else:
        model = nn.DataParallel(model,device_ids=[1]) # GPU 1
    model = model.to(device)
    MILoss = DeepMILoss(args.weight_miloss,args.weight_LMI,args.weight_GMI).to(device)
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    optimizer_miloss = optim.Adam(MILoss.parameters(), lr=args.lr_MMI)
    criterion = {
        "cls": SoftmaxFocalClassificationLoss(),
        "loc": WeightedSmoothL1LocalizationLoss(),
    }

    if args.kd_flag == 1:
        teacher = TeacherNet(config)
        if flag_GPU == 0:
            teacher = nn.DataParallel(teacher,device_ids=[0])
        else:
            teacher = nn.DataParallel(teacher,device_ids=[1]) # GPU 1
        teacher = teacher.to(device)
        faf_module = FaFModule(
            model, teacher, config, optimizer, criterion, args.kd_flag,MMI_flag,MILoss,optimizer_miloss
        )
        checkpoint_teacher = torch.load(args.resume_teacher,map_location='cuda:%d'%flag_GPU)
        start_epoch_teacher = checkpoint_teacher["epoch"]
        faf_module.teacher.load_state_dict(checkpoint_teacher["model_state_dict"])
        print(
            "Load teacher model from {}, at epoch {}".format(
                args.resume_teacher, start_epoch_teacher
            )
        )
        faf_module.teacher.eval()
    else:
        faf_module = FaFModule(model, model, config, optimizer, criterion, args.kd_flag, MMI_flag,MILoss,optimizer_miloss)

    rsu_path = "with_rsu" if args.rsu else "no_rsu"
    model_save_path = check_folder(logger_root)
    # model_save_path = check_folder(os.path.join(model_save_path, flag))

    if flag != "disco": #
        str_kd_disco = ""
    elif args.kd_flag: # disco kd
        str_kd_disco = ""
    else: # disco，no kd
        str_kd_disco = "+no_kd"

    flag_directory = flag + str_kd_disco + ("" if not MMI_flag else "+MMI+" + str(args.alpha))
    model_save_path = check_folder(os.path.join(model_save_path, flag_directory))
    if args.rsu:
        model_save_path = check_folder(os.path.join(model_save_path, "with_rsu"))
    else:
        model_save_path = check_folder(os.path.join(model_save_path, "no_rsu"))

    # check if there is valid check point file
    has_valid_pth = False
    for pth_file in os.listdir(os.path.join(auto_resume_path, f"{flag_directory}/{rsu_path}")):
        if pth_file.startswith("epoch_") and pth_file.endswith(".pth"):
            has_valid_pth = True
            break

    if not has_valid_pth:
        print(
            f"No valid check point file in {auto_resume_path} dir, weights not loaded."
        )
        auto_resume_path = ""

    if MMI_flag:
        model_save_path_miloss = os.path.join(model_save_path,"model_MILoss")
        check_folder(model_save_path_miloss)
    if args.resume == "" and auto_resume_path == "":
        log_file_name = os.path.join(model_save_path, "log.txt")
        # 2023年4月22日 防止已有文件被清空
        if os.path.isfile(log_file_name):
            saver = open(log_file_name, "a")
        else:
            saver = open(log_file_name, "w")
        saver.write("\nGPU number: {}\n".format(torch.cuda.device_count()))
        saver.flush()

        # Logging the details for this experiment
        saver.write("command line: {}\n".format(" ".join(sys.argv[0:])))
        saver.write(args.__repr__() + "\n\n")
        saver.flush()
    else:
        if auto_resume_path != "":
            model_save_path = os.path.join(auto_resume_path, f"{flag_directory}/{rsu_path}")
        else:
            model_save_path = args.resume[: args.resume.rfind("/")].replace(flag,flag_directory)

        log_file_name = os.path.join(model_save_path, "log.txt")

        if os.path.exists(log_file_name):
            saver = open(log_file_name, "a")
        else:
            os.makedirs(model_save_path, exist_ok=True)
            saver = open(log_file_name, "w")

        saver.write("GPU number: {}\n".format(torch.cuda.device_count()))
        saver.flush()

        # Logging the details for this experiment
        saver.write("command line: {}\n".format(" ".join(sys.argv[1:])))
        saver.write(args.__repr__() + "\n\n")
        saver.flush()

        if auto_resume_path != "":
            list_of_files = glob.glob(f"{model_save_path}/*.pth")
            latest_pth = max(list_of_files, key=os.path.getctime)
            checkpoint = torch.load(latest_pth,map_location='cuda:%d'%flag_GPU)


            # MILOSS model
            if MMI_flag:
                list_of_files_ = glob.glob(f"{model_save_path_miloss}/*.pth")
                latest_pth_ = max(list_of_files_, key=os.path.getctime)
                checkpoint_ = torch.load(latest_pth_,map_location='cuda:%d'%flag_GPU)
        else:
            checkpoint = torch.load(args.resume,map_location='cuda:%d'%flag_GPU)

        start_epoch = checkpoint["epoch"] + 1
        faf_module.model.load_state_dict(checkpoint["model_state_dict"])
        faf_module.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        faf_module.scheduler.load_state_dict(checkpoint["scheduler_state_dict"])

        if MMI_flag:
            faf_module.MILoss.load_state_dict(checkpoint_["model_state_dict"])
            faf_module.optimizer_miloss.load_state_dict(checkpoint_["optimizer_state_dict"])
            faf_module.scheduler_miloss.load_state_dict(checkpoint_["scheduler_state_dict"])

        print("Load model from {}, at epoch {}".format(args.resume, start_epoch - 1))

    print(f"model save path: {model_save_path}")

    for epoch in range(start_epoch, num_epochs + 1):
        lr = faf_module.optimizer.param_groups[0]["lr"]
        if MMI_flag:
            lr_MMI = faf_module.optimizer_miloss.param_groups[0]["lr"]
        print("Epoch {}, learning rate {}".format(epoch, lr))

        if need_log:
            if MMI_flag:
                saver.write("epoch: {}, lr: {}/{}\t".format(epoch, lr, lr_MMI))
            else:
                saver.write("epoch: {}, lr: {}\t".format(epoch, lr))
            saver.flush()

        if MMI_flag:
            running_loss_mi = AverageMeter("mi loss", ":.6f")
        running_loss_disp = AverageMeter("Total loss", ":.6f")
        running_loss_class = AverageMeter(
            "classification Loss", ":.6f"
        )  # for cell classification error
        running_loss_loc = AverageMeter(
            "Localization Loss", ":.6f"
        )  # for state estimation error

        faf_module.model.train()

        t = tqdm(training_data_loader)
        for sample in t:
            (
                padded_voxel_point_list,  # voxelized point cloud for individual agent
                padded_voxel_points_teacher_list,  # fused voxelized point cloud for all agents (multi-view)
                label_one_hot_list,  # one hot labels
                reg_target_list,  # regression targets
                reg_loss_mask_list,
                anchors_map_list,  # anchor boxes
                vis_maps_list,
                target_agent_id_list,
                num_agent_list,  # e.g. 6 agent in current scene: [6,6,6,6,6,6], 5 agent in current scene: [5,5,5,5,5,0]
                trans_matrices_list,  # matrix for coordinate transformation. e.g. [batch_idx, j, i] ==> transformation matrix to transfer from agent i to j
            ) = zip(*sample)

            trans_matrices = torch.stack(tuple(trans_matrices_list), 1)
            target_agent_id = torch.stack(tuple(target_agent_id_list), 1)
            num_all_agents = torch.stack(tuple(num_agent_list), 1)

            # add pose noise
            if pose_noise > 0:
                apply_pose_noise(pose_noise, trans_matrices)

            if not args.rsu:
                num_all_agents -= 1

            if flag == "upperbound":
                padded_voxel_point = torch.cat(
                    tuple(padded_voxel_points_teacher_list), 0
                )
            else:
                padded_voxel_point = torch.cat(tuple(padded_voxel_point_list), 0)

            label_one_hot = torch.cat(tuple(label_one_hot_list), 0)
            reg_target = torch.cat(tuple(reg_target_list), 0)
            reg_loss_mask = torch.cat(tuple(reg_loss_mask_list), 0)
            anchors_map = torch.cat(tuple(anchors_map_list), 0)
            vis_maps = torch.cat(tuple(vis_maps_list), 0)

            data = {
                "bev_seq": padded_voxel_point.to(device),
                "labels": label_one_hot.to(device),
                "reg_targets": reg_target.to(device),
                "anchors": anchors_map.to(device),
                "reg_loss_mask": reg_loss_mask.to(device).type(dtype=torch.bool),
                "vis_maps": vis_maps.to(device),
                "target_agent_ids": target_agent_id.to(device),
                "num_agent": num_all_agents.to(device),
                "trans_matrices": trans_matrices,
            }

            if args.kd_flag == 1:
                padded_voxel_points_teacher = torch.cat(
                    tuple(padded_voxel_points_teacher_list), 0
                )
                data["bev_seq_teacher"] = padded_voxel_points_teacher.to(device)
                data["kd_weight"] = args.kd_weight

            loss, cls_loss, loc_loss, mi_loss = faf_module.step(
                data, batch_size, num_agent=num_agent,alpha=args.alpha
            )
            if MMI_flag:
                running_loss_mi.update(mi_loss)
            running_loss_disp.update(loss)
            running_loss_class.update(cls_loss)
            running_loss_loc.update(loc_loss)

            if np.isnan(loss) or np.isnan(cls_loss) or np.isnan(loc_loss):
                print(f"Epoch {epoch}, loss is nan: {loss}, {cls_loss} {loc_loss}")
                sys.exit()

            if MMI_flag:
                t.set_description("Epoch {},     lr {}/{}".format(epoch, lr, lr_MMI))
            else:
                t.set_description("Epoch {},     lr {}".format(epoch, lr))

            if MMI_flag:
                t.set_postfix(
                cls_loss=running_loss_class.avg, loc_loss=running_loss_loc.avg, mi_loss = running_loss_mi.avg
            )
            else:
                t.set_postfix(
                cls_loss=running_loss_class.avg, loc_loss=running_loss_loc.avg
            )


        faf_module.scheduler.step()
        if MMI_flag:
            faf_module.scheduler_miloss.step()

        # save model
        if need_log:
            if MMI_flag:
               saver.write(
                    "{}\t{}\t{}\t{}\n".format(
                        running_loss_disp, running_loss_class, running_loss_loc,running_loss_mi
                    )
                )
            else:
                saver.write(
                    "{}\t{}\t{}\n".format(
                        running_loss_disp, running_loss_class, running_loss_loc
                    )
                )
            saver.flush()
            if config.MGDA:
                save_dict = {
                    "epoch": epoch,
                    "encoder_state_dict": faf_module.encoder.state_dict(),
                    "optimizer_encoder_state_dict": faf_module.optimizer_encoder.state_dict(),
                    "scheduler_encoder_state_dict": faf_module.scheduler_encoder.state_dict(),
                    "head_state_dict": faf_module.head.state_dict(),
                    "optimizer_head_state_dict": faf_module.optimizer_head.state_dict(),
                    "scheduler_head_state_dict": faf_module.scheduler_head.state_dict(),
                    "loss": running_loss_disp.avg,
                }
            else:
                save_dict = {
                    "epoch": epoch,
                    "model_state_dict": faf_module.model.state_dict(),
                    "optimizer_state_dict": faf_module.optimizer.state_dict(),
                    "scheduler_state_dict": faf_module.scheduler.state_dict(),
                    "loss": running_loss_disp.avg,
                }
                if MMI_flag:
                    save_dict_miloss = {
                        "epoch": epoch,
                        "model_state_dict": faf_module.MILoss.state_dict(),
                        "optimizer_state_dict": faf_module.optimizer_miloss.state_dict(),
                        "scheduler_state_dict": faf_module.scheduler_miloss.state_dict(),
                        "loss": running_loss_disp.avg,
                    }

            flag_save = False
            flag_save_mi = False
            if epoch<=110:
                if epoch%10==0:
                    flag_save=True
                    flag_save_mi=True
            else:
                flag_save=True
                flag_save_mi=True

            # flag_save = True
            if flag_save:
                torch.save(
                    save_dict, os.path.join(model_save_path, "epoch_" + str(epoch) + ".pth")
                )
            if flag_save_mi:
                if MMI_flag:
                    torch.save(
                        save_dict_miloss, os.path.join(model_save_path_miloss, "mi_epoch_" + str(epoch) + ".pth")
                    )

    if need_log:
        saver.close()

    # if args.kd_flag:
    #     cal_kd_loss_from_log(args.logpath,args.com,args.rsu)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d",
        "--data",
        default=None,
        type=str,
        help="The path to the preprocessed sparse BEV training data",
    )
    parser.add_argument("--batch_size", default=4, type=int, help="Batch size")
    parser.add_argument("--nepoch", default=100, type=int, help="Number of epochs")
    parser.add_argument("--nworker", default=4, type=int, help="Number of workers")
    parser.add_argument("--lr", default=0.001, type=float, help="Initial learning rate")
    parser.add_argument("--log", action="store_true", help="Whether to log")
    parser.add_argument("--logpath", default="", help="The path to the output log file")
    parser.add_argument(
        "--resume",
        default="",
        type=str,
        help="The path to the saved model that is loaded to resume training",
    )
    parser.add_argument(
        "--resume_teacher",
        default="",
        type=str,
        help="The path to the saved teacher model that is loaded to resume training",
    )
    parser.add_argument(
        "--layer",
        default=3,
        type=int,
        help="Communicate which layer in the single layer com mode",
    )
    parser.add_argument(
        "--warp_flag", default=0, type=int, help="Whether to use pose info for When2com"
    )
    parser.add_argument(
        "--kd_flag",
        default=0,
        type=int,
        help="Whether to enable distillation (only DiscNet is 1 )",
    )
    parser.add_argument("--kd_weight", default=100000, type=int, help="KD loss weight")
    parser.add_argument(
        "--gnn_iter_times",
        default=3,
        type=int,
        help="Number of message passing for V2VNet",
    )
    parser.add_argument(
        "--visualization", default=True, help="Visualize validation result"
    )
    parser.add_argument(
        "--com",
        default="",
        type=str,
        help="lowerbound/upperbound/disco/when2com/v2v/sum/mean/max/cat/agent",
    )
    parser.add_argument("--rsu", default=0, type=int, help="0: no RSU, 1: RSU")
    parser.add_argument(
        "--num_agent", default=5, type=int, help="The total number of agents"
    )
    parser.add_argument(
        "--auto_resume_path",
        default="",
        type=str,
        help="The path to automatically reload the latest pth",
    )
    parser.add_argument(
        "--compress_level",
        default=0,
        type=int,
        help="Compress the communication layer channels by 2**x times in encoder",
    )
    parser.add_argument(
        "--pose_noise",
        default=0,
        type=float,
        help="draw noise from normal distribution with given mean (in meters), apply to transformation matrix.",
    )
    parser.add_argument(
        "--only_v2i",
        default=0,
        type=int,
        help="1: only v2i, 0: v2v and v2i",
    )

    parser.add_argument("--flag_GPU",default=0,type=int)
    parser.add_argument("--MMI_flag",default=0,type=int,help="Whether to enable MMI",)
    parser.add_argument("--alpha",default=0,type=float)
    parser.add_argument("--weight_miloss",default=100,type=int,help="(L+G)total mi loss weight")
    parser.add_argument("--weight_LMI",default=0.5,type=float)
    parser.add_argument("--weight_GMI",default=0.5,type=float)
    parser.add_argument("--lr_MMI",default=0.001,type=float)
    parser.add_argument("--seed",default=1,type=int)

    torch.multiprocessing.set_sharing_strategy("file_system")
    args = parser.parse_args()

    # # debug params
    # args.data ='coperception_old/data/V2X-Sim-1-det/train'
    # args.com = 'disco'
    # args.batch = 4
    # args.kd_flag =0

    # args.nepoch = 160
    # args.rsu = 1
    # args.flag_GPU = 0

    # args.MMI_flag = 1
    # args.weight_miloss = 100
    # args.weight_LMI = 0.5
    # args.weight_GMI = 0.5
    # args.alpha = 0.5
    # args.lr_MMI = 0.001

    # args.log = False
    # args.seed = 1

    # args.logpath = 'coperception_origin/tools/det/logs_v1_hyperpara_seed{}/logs_lr{}_{}_L{}+G{}'.format(
    #     args.seed, args.lr_MMI, args.weight_miloss, args.weight_LMI, args.weight_GMI)
    # args.auto_resume_path = args.logpath
    # =============

    print(args)
    if args.MMI_flag:
        torch.manual_seed(args.seed)
        random.seed(args.seed)

    torch.manual_seed(args.seed)
    random.seed(args.seed)

    os.environ['PYTHONHASHSEED'] =str(args.seed)
    np.random.seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    # torch.backends.cudnn.benchmark=False
    # torch.backends.cudnn.deterministic =True

    main(args)
