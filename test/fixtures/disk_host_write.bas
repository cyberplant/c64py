5 print "hello world"
10 rem c64py fixture: create seq file then save this program as prg
20 open1,8,2,"HOSTSEQ,S,W"
30 print#1,"hello-from-basic"
40 close1
50 save "HOSTPRG",8
60 print "write ok"
