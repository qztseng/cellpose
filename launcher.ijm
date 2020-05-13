//exec("/home/lis-paul/miniconda3/condabin/conda", "activate", "cellpose");
//exec("/home/lis-paul/miniconda3/condabin/conda", "env", "list");
//exec("python", "-c", "import numpy;print(numpy.__version__)");
//exec("ps", "-aux");
exec("python -m cellpose --use_gpu --dir /home/lis-paul/cellpose/test_image/ --pretrained_model nuclei --diameter 0. --save_png");
//exec("python", "-m", "cellpose --use_gpu --dir /home/lis-paul/cellpose/test_image/", --pretrained_model nuclei --diameter 0. --save_png");
//python -m cellpose --dir /home/lis-paul/cellpose/test_image/ --pretrained_model nuclei --diameter 0. --save_png
