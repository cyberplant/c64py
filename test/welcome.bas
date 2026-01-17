10 rem initialize sid chip
15 poke 54296,15:rem volume max
20 rem startup sound effect
25 poke 54277,0:poke 54278,240:rem fast attack, full sustain
30 for fh=1 to 8
35 for fl=0 to 255 step 16
40 poke 54272,fl:poke 54273,fh
45 poke 54276,33:rem sawtooth wave + gate on
50 for d=1 to 2:next d
55 next fl
60 next fh
65 poke 54276,32:rem gate off
70 for d=1 to 30:next d
75 for x=1 to 20
80 print "welcome to sys-64738.com"
85 for y=1 to 200
90 next y
95 poke 53280, x
96 poke 646, x+1
97 rem short beep on color change
98 poke 54272,100:poke 54273,2
99 poke 54277,0:poke 54278,240
100 poke 54276,33:rem sawtooth wave + gate on
101 for d=1 to 5:next d
102 poke 54276,32:rem gate off
105 next x
110 rem ending sound effect
115 poke 54277,0:poke 54278,240
120 for fh=8 to 1 step -1
125 for fl=255 to 0 step -16
130 poke 54272,fl:poke 54273,fh
135 poke 54276,33:rem sawtooth wave + gate on
140 for d=1 to 2:next d
145 next fl
150 next fh
155 poke 54276,32:rem gate off
160 print "see you later"
165 for d=1 to 100:next d
1000 sys 64738
