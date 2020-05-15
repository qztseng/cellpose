//dir = getDirectory("Choose a Directory");
dir = '/home/lis-paul/cellpose/notebooks/output/';
prefix = 'px_';

setBatchMode(true);
flist = getFileList(dir);
for (i = 0; i < 30; i++) {
	//print(flist[i]);
	fname = prefix+i+'.txt';
	run("Text Image... ", "open="+dir+fname+"");
	if (i!=0){
		run("Concatenate...", "  title="+prefix+"0.txt image1="+prefix+"0.txt image2="+fname+"");
	}
}
setBatchMode('exit and display');
run("mpl-magma");