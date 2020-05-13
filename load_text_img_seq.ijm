//dir = getDirectory("Choose a Directory");
dir = '/home/lis-paul/cellpose/notebooks/output/';
prefix = 't_';

setBatchMode(true);
flist = getFileList(dir);
for (i = 0; i < lengthOf(flist); i++) {
	//print(flist[i]);
	fname = prefix+i+'.txt';
	run("Text Image... ", "open="+dir+fname+"");
	if (i!=0){
		run("Concatenate...", "  title=t_0.txt image1=t_0.txt image2="+fname+"");
	}
}
setBatchMode('exit and display');
run("mpl-magma");