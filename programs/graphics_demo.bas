10 rem *** c64 graphics demo ***
20 rem by copilot - comprehensive test
30 print chr$(147):rem clear screen
40 print "graphics mode demo"
50 print "==================="
60 print
70 print "this demo will show:"
80 print "1. bitmap mode"
90 print "2. patterns and shapes"
100 print "3. animated sprites"
110 print
120 print "press any key to start..."
130 get k$:if k$="" then 130
135 gosub 5000
140 rem
150 rem *** setup bitmap mode ***
160 rem
170 poke 53280,0:rem border=black
180 poke 53281,6:rem background=blue
185 gosub 5000
190 rem
200 rem enable bitmap mode
210 poke 53265,peek(53265) or 32
220 rem set bitmap at $2000, screen at $0400
230 poke 53272,8
235 gosub 5000
240 rem
250 rem *** clear bitmap ***
260 rem
270 for i=8192 to 16191
280 poke i,0
290 next i

295 gosub 5000
300 rem
310 rem *** set colors ***
320 rem yellow (7) on blue (6) = $76
330 rem
340 for i=1024 to 2023
350 poke i,118:rem $76
360 next i
365 gosub 5000
370 rem
380 rem *** draw border box ***
390 rem
400 rem top line
410 for i=0 to 39
420 poke 8192+i,255
430 next i
440 rem bottom line
450 for i=0 to 39
460 poke 8192+7960+i,255
470 next i
480 rem left and right edges
490 for y=0 to 199
500 poke 8192+y*40,128:rem left
510 poke 8192+y*40+39,1:rem right
520 next y
525 gosub 5000
530 rem
540 rem *** draw diagonal lines ***
550 rem
560 for i=0 to 100
570 x=i:y=i
580 byte=8192+y*40+int(x/8)
590 bit=7-(x and 7)
600 poke byte,peek(byte) or (2^bit)
610 next i
620 rem
630 rem second diagonal
640 for i=0 to 100
650 x=i:y=199-i
660 byte=8192+y*40+int(x/8)
670 bit=7-(x and 7)
680 poke byte,peek(byte) or (2^bit)
690 next i
700 rem
710 rem *** draw circles (approximation) ***
720 rem
730 cx=160:cy=100:r=30
740 for a=0 to 360 step 5
750 x=cx+r*cos(a*3.14159/180)
760 y=cy+r*sin(a*3.14159/180)
770 if x<0 or x>319 or y<0 or y>199 then 800
780 byte=8192+int(y)*40+int(x/8)
790 bit=7-(int(x) and 7)
800 poke byte,peek(byte) or (2^bit)
810 next a
820 rem
830 rem *** setup sprites ***
840 rem
850 rem enable sprite 0
860 poke 53269,1
870 rem set color to white
880 poke 53287,1
890 rem set sprite pointer
900 poke 2040,128:rem sprite data at $2000
910 rem
920 rem create simple sprite (ball)
930 for i=0 to 62
940 poke 8192+i,0
950 next i
960 rem draw a ball
970 poke 8192+0,0:poke 8192+1,60:poke 8192+2,0
980 poke 8192+3,0:poke 8192+4,126:poke 8192+5,0
990 poke 8192+6,1:poke 8192+7,255:poke 8192+8,128
1000 poke 8192+9,3:poke 8192+10,255:poke 8192+11,192
1010 poke 8192+12,7:poke 8192+13,255:poke 8192+14,224
1020 poke 8192+15,15:poke 8192+16,255:poke 8192+17,240
1030 poke 8192+18,31:poke 8192+19,255:poke 8192+20,248
1040 rem
1050 rem *** animation loop ***
1060 rem
1070 x=50:y=100:dx=2:dy=1
1080 rem
1090 rem main loop
1100 rem
1110 x=x+dx
1120 if x>280 or x<24 then dx=-dx
1130 y=y+dy
1140 if y>220 or y<50 then dy=-dy
1150 rem
1160 poke 53248,x:rem sprite x
1170 poke 53249,y:rem sprite y
1180 rem
1190 rem small delay
1200 for d=1 to 100:next d
1210 rem
1220 rem check for keypress
1230 get k$:if k$="" then 1110
1240 rem
1250 rem *** restore text mode ***
1260 rem
1270 poke 53265,peek(53265) and 223
1280 poke 53269,0:rem disable sprites
1290 print chr$(147):rem clear screen
1300 print "demo complete!"
1310 print
1320 print "graphics features tested:"
1330 print "- bitmap mode (320x200)"
1340 print "- border box"
1350 print "- diagonal lines"
1360 print "- circle (approximation)"
1370 print "- animated sprite"
1380 end
5000 for i = 1 to 50
5002 print i
5005 poke 53280, i
5009 next
5010 return
