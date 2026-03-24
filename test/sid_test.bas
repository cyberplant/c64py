10 rem sid_test.bas - predictable SID register test patterns
20 s=54272:rem $D400 base
30 poke s+24,15:rem $D418 volume max
40 rem set ADSR (fast attack, short decay, full sustain, short release)
50 poke s+5,0:poke s+6,240
60 rem ---- triangle tone sweep ----
70 print "triangle sweep"
80 for f=400 to 8000 step 200
90 poke s+0,f and 255:poke s+1,int(f/256)
100 poke s+4,17:rem triangle (16) + gate on (1)
110 for d=1 to 50:next d
120 poke s+4,16:rem gate off
130 for d=1 to 10:next d
140 next f
150 poke s+4,0
160 rem ---- saw tone sweep ----
170 print "saw sweep"
180 for f=400 to 8000 step 200
190 poke s+0,f and 255:poke s+1,int(f/256)
200 poke s+4,33:rem saw (32) + gate on (1)
210 for d=1 to 50:next d
220 poke s+4,32:rem gate off
230 for d=1 to 10:next d
240 next f
250 poke s+4,0
260 rem ---- pulse width sweep at fixed pitch ----
270 print "pulse width sweep"
280 f=3000:poke s+0,f and 255:poke s+1,int(f/256)
290 for pw=256 to 3840 step 128
300 poke s+2,pw and 255:poke s+3,int(pw/256)
310 poke s+4,65:rem pulse (64) + gate on (1)
320 for d=1 to 35:next d
330 next pw
340 poke s+4,64:rem gate off
350 rem ---- noise burst ----
360 print "noise burst"
370 poke s+4,129:rem noise (128) + gate on (1)
380 for d=1 to 200:next d
390 poke s+4,128:rem gate off
400 print "done"
410 end
