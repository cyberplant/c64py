10 rem c64py fixture: read seq written by disk_host_write.bas
20 open1,8,2,"HOSTSEQ,S,R"
30 line input#1,a$
40 close1
50 print a$
60 rem rel files: not exercised here (needs rel open syntax + host rel support)
