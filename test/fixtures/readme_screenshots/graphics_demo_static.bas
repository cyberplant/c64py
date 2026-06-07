10 rem static frame for readme screenshot (no input waits)
20 poke 53280,0:rem border black
30 poke 53281,6:rem background blue
40 poke 53265,peek(53265) or 32
50 poke 53272,8
60 for i=8192 to 16191:poke i,0:next
70 for i=1024 to 2023:poke i,118:next
80 for i=0 to 39:poke 8192+i,255:next
90 for i=0 to 39:poke 8192+7960+i,255:next
100 for y=0 to 199
110 poke 8192+y*40,128
120 poke 8192+y*40+39,1
130 next y
140 poke 53269,1
150 poke 53287,1
160 poke 2040,128
170 poke 8192+7,255:poke 8192+8,128
180 poke 8192+9,255:poke 8192+10,255
190 poke 8192+12,255:poke 8192+13,255
200 poke 8192+15,255:poke 8192+16,255
210 poke 53248,120:poke 53249,90
220 end
