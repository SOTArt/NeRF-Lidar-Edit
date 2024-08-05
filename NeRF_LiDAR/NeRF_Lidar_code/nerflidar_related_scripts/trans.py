import os

# scenes = [16,29,33,38,60]
scenes = [33,52]

# exps =['baseline','nodepth']
exps =['weights{}'.format(i) for i in range(1,5)]

for exp in exps:
    for scene in scenes:
        os.system('cp -r ./Snerf/exp/lidar_rendering_ablation/scene{}/scene{}_{}_lidar_simulation ./simulation_ablation'.format(scene,scene,exp))
        # 递归复制文件路径，.format中的变量值依次补全{}中的值，构造完整路径
