# deadmau5 — Autopilot walkthrough transcript

Source: https://www.youtube.com/watch?v=A2lwxhFPPDI
Transcribed via Gemini 2.5 Pro (Vertex) on 2026-05-25. Note: truncated by model output-token limit near ~[1:06:05] (video tail may be missing).

Here is a complete transcript of the video.

[00:00] Alright, here we go. Uh, probably a better recording, uh, because now you can actually hear me talk and see my mouse, uh, which I totally realized that it wasn't showing my mouse cursor in the last video. But this is better because now I can explain it.

[00:15] Um, so this is the application, for lack of a better working title, uh, is gonna be called Autopilot. Um, and as we, when we start, we the, the application starts up in one of four modes, right? This is Perform mode, uh, that we're looking at here.

[00:32] Um, so to change modes, uh, you know, uh, it's up at the top left, a little dropdown. Uh, again, this probably a UI refactor in the near future for me, but, uh, we have four modes. We have Edit, Perform, uh, Arrange, and, and Catalog, right? Um, they're all kind of self-explanatory, but we'll, we'll work right now, we'll just show you the modes.

[01:00] [The user is clicking through the main application modes: Edit, Perform, Arrange, Catalog.]
...we'll just show you the modes. Um, and I'm showing you all the stuff up in the header right now, right? So the header is just this top part. Um, show/hide the browser, show/hide the Master section. This is gonna be important later because of just, you know, the controllerism element that's, you know, being integrated into this, which we'll explain later. Uhm, 'cause when you're performing, like, if you've got a really great setup and you've got this thing finessed, like, you don't even need to see the Master section. It's, it's just kind of there.

[01:21] Um, and then our metronome... [Clicks the metronome button, which starts and stops a click track] which is, as one would expect, is a metronome. And let me explain something about this clock, right? You know, we're just turning it on and off there, and then there's a volume control for it. But, um, I, I, I initially kind of just started this whole project off with like having that clock running with a perfectly, like, you know, really sample-accurate, uh, like buffer position-accurate, uh, metronome so I could make sure that everything, you know, I was working on was in time and everything was working like as it should.

[02:00] Um, so, you know, we've got that, and then we've got our clock here, uh, and our BPM set. This clock and this bar/beat indicator, it's just kind of there for it being there. It really serves no purpose other than to tell you kind of maybe where you are because the way that this application works is this clock is running all the time. It just starts running when you open the app and it never stops. There's no transport, there's no play/stop kind of thing, in Perform mode for, for a reason, which you'll probably figure out later.

[02:26] Um, so the clock is just basically like kind of a wall clock. It's the ground truth of everything. Um, and then our quantize values, which we'll talk about how these work. And then just a master volume thing. Again. Uh, and then just a little beat clock indicator. And then, uh, MIDI and OSC activity lights. So that, like for example, if I'm sending OSC, if I go over to TouchDesigner, right, and I uh... [Drags the application window to the side to reveal TouchDesigner] Okay, see the activity light lit up? And that's all great. So that just means we're getting OSC. Uh, at a glance.

[03:06] You know, so the header will always be showing the whole time. Uh, so, you know, even as we... I gotta figure this out with the Mac so it stops dragging that stupid thing down. [Struggles with the application's top menu bar interaction on macOS] Um, so yeah, you know, if we're in Arrange or Catalog or whatever, the header's always there doing something, right? But let's go into Perform mode and let's just add a track, right, so we can kind of get a feel how this works, right?

[03:30] [User drags an .aiff file from a Finder window into the "DROP WAV, AIFF, OR MP3 FILES HERE" area.]
Uh, so let's just take this track, right? Now, these are just AIFFs, nothing special about them, downloaded from Beatport, whatever. Wave files are fine, AIFF, MP3 files, like, I'm gonna work on that a bit more. It's, there's a lot to do with like transcoding, and MP3s like kind of, there's a little magic you have to do to get an MP3 to play, like inside our framework because it has to transcode it and that means it's a heavier process than a wave file. Um, and all that stuff. So I'm just kind of avoiding that, and you shouldn't be playing MP3s anyway, shame on you. Uh, play waves or AIFFs. AIFFs I like better just because of all the metadata and cool shit.

[04:11] Anyway, let's take a track and drag it in. Okay. [Drags a file "Timo Vaarsta, Hankkheimer - Oscilate.aiff" into the track browser.] Now, we just saw a dialogue pop up for like a split second. What that actually did is it processed the track automatically. So, by "processed," it, it pulled the album artwork here, it generated a waveform thumbnail, like a big one, too, in a split second. It's so fast, I loved it.

[04:31] Um, so we can see things like, you know, the details of the wave. It's kind of, you know, just a benign gray waveform right now. BPM, it automatically detects the BPM. So if there's BPM written in the metadata, that's fine, it'll use that. But if there's not, that's also fine, it'll do its best to detect it. And that, actually, that's courtesy of Steve Duda at uh, Xfer Records, uh, Serum guy, you might know him. He, uh, wrote, helped me, uh, well, actually, he wrote it all, actually, the BPM detection for it, and it's fucking great. It's really reliable. It has yet to fuck me over on getting a wrong BPM for like a techno or a house track or a drum and bass track. So, fucking shout out to Steve Duda.

[05:11] Uh, you know, and then it just pulled in the metadata that was written in that AIFF. So if you've downloaded the track from Beatport, chances are it was probably packed with a ton of stuff. So, let's just talk about this really quick, like this browser bit, right? We've only got one track in here, but we can do this. [Drags a large selection of files into the browser.] We can, uhm, we can grab a whole bunch of tracks, right? [The "IMPORTING TRACKS" dialog appears and processes hundreds of files.]

[05:38] So now that, this is gonna take a little longer because it's, it's doing the BPM analysis, waveform generation, artwork extraction, blah blah blah on 200 tracks, but it just did it. So, there's 200 tracks, like no problem.

[05:51] Um, and the technology that we're using for the UI is just so great. Um, it's super responsive. It runs 60Hz no sweat with ProMotion on a Mac, and then 120 FPS plus on a PC with like a half-decent GPU. So, I'm super stoked about that. Um, and you'll see the advantage for that for a while.

[06:14] [User adjusts the row height of the track browser using a slider.]
So, you've got old-man-Joel mode where you can like, you know, zoom in. "I can't read the font!" Um, or you've got little-baby-Martin-Garrix mode where he's got perfect vision and he can see these things at 8 points. Anyway. So, here we've got tracks, right? And we've added tracks and you can always right-click, remove it, and just, you know, basic bitch shit that you would expect from a browser.

[06:35] Uh, we can sort, uh, by columns, by label, uh, you know, by artist and stuff like that. We could do searches. So if I search for, if I start typing "s-e-a-r-c-h"... yeah, there you go. So I've got a track called, uh, I don't even know why it started... Tesseract, right? It found it in the title. So if you've got the hint of a name, like if I know I've got a track called "mouth"... oh, but I probably didn't import it.

[07:04] Anyway, let's look at a track, "Figure". So if I type the word "figure"... there you go. The label "Figure", you know, so it's gonna look through all the metadata. So these are the columns we're showing. So if we go to column filter, now we can like, now we can only show, it's just basic browser shit, dude, you guys are gonna figure it out, I don't have to explain all of it to you. But you can show/hide columns, all that stuff too, if you don't need to know the key, the rating, and all that dog shit. Oh, note to self, I have to finish the rating system. I haven't even done that yet.

[08:06] Alright, so we go into Edit mode because we need to... it's really important, uh, for this to work great, to author tracks. Um, make edits, and, uh, not make edits, but just, um, configure the tracks properly so that, you know, they perform the best way that they can, uh, and there's no sync issues and stuff like that.

[08:40] So, basically what we're gonna do is, we'll go in and we'll look, uh, at how to do this. So, like I'm going to sort these tracks, I'd say by BPM. Uh, so I'm gonna go... I'm gonna take this, uh... Live For Yesterday (Original Mix). So now we're in the editor.

[09:24] [The user has double-clicked a track to load it into the Edit Mode waveform view.]
And hitting spacebar overrides the, the preview, or whatever. You'll figure it out. It's you're gonna find it's very similar to some other software that we won't ever mention ever again, hopefully. Um, so, you know, I'm playing it, see my waveform scrolling, looks great, and all that. Uh, we can scroll right in, get very sample-accurate with our cue positions.

[09:47] Now, by default, it had added the first beat grid marker, just like Rekordbox kind of tries to do. Uh, but what mine does is it adds it right at the beginning of the file, just, you know, assuming that's where it goes, right? And I wanna say conservatively 60% of the time, the marker should be right at zero. And the way that we can tell, you know, is just by, just how you set grid points in other apps, is turn the metronome on.

[10:17] [The user plays the track with the metronome, adjusting the grid marker to align with the kick drum.]
Yeah, it sounds good. I mean, it actually sounds a little early to me. So, um, you know, I'm gonna use these like little nudge tools. And you see I just moved one up a little bit. And alternatively, the playhead, as you click and drag here and you move the playhead, and then you hit the grid button, it just moves the grid to the playhead. So, let's stick it right at that start of that transient, which is like right here, right? So it's like a, like a couple, like maybe it looks like 300 samples in and then it starts, right?

[10:54] Yeah, that sounds really tight to me. And so, as you can see, our grid lines up, we can see the bar, the beat, and all that stuff, right? So, let's make this a little more interesting, right? Let's go to over to the start of the track. You can navigate the track just by, you know, dragging on the overview waveform at the bottom here. So we're gonna go to the beginning here. And we're gonna add a cue, right? So we've got 16 cues. Uh, we're gonna just hit boom, one, right? Okay, so we've added it, but my cursor, my playhead was at there, so it's gonna add cues to the nearest quantize value of where your playhead is, which is this white line in the middle, right? So if I add two here, it's gonna chuck it over there. It's just gonna snap it to the grid because, really, that's probably what you're gonna do.

[11:47] You can drag cues around and modify them, and you'll, you'll also notice there's some goofy coloring happening here and I'll explain why in a second and why this system's so great. So, I'm gonna go here and I'm gonna move this to one 'cause this is gonna be my first cue. And you can hit the cue, you know, mash that button, that's good and great, that's where that cue goes. And then there's the other cue. Cool.

[12:13] Alright, so now we've... look at our waveform, changed all purple because, you know, cue 2 just goes all the way to whatever. Uh, so let's go find a breakdown actually, instead of just doing it like that. So, uh, I can go here... I know there's the breakdown. So, it's at bar 45. Uh, I'm actually gonna delete that by... I think you hit Control. Yeah. Yeah, and then we go to bar 45. And then we'll go to bar here.

[12:44] Oh, and you can do this while it's playing as well. So I'm just gonna add a couple of cues.

[13:58] [The user adds multiple cue points along the track's timeline, which causes the waveform display to change colors, segmenting the track visually.]
Okay, great. So, now you can see our track got colored down here, right? So we can, we can very clearly see these sections. Now, the coloring isn't gonna be this goofy. It can, you can customize this like fully. Um, where if we go into Settings, and we'll, we'll go over more settings later. So if you go into General... I'm probably gonna figure this out. You go to Cue Colors, Custom, and I'm just gonna shuffle these, right? But that's a custom palette. So we can go through and we can find, you know, all these different... I'm gonna make some more, I think. Like, different like sets of cue colors for whatever reason. I really like "Sunset". Now, yeah, sure, like that's the gradient, so like it'll flow in that direction. But, or you can reverse it. But I like shuffling it.

[15:51] [User cycles through different color palettes for the cue points, which changes the colors of the waveform sections.]
Um, because when you shuffle a, a palette like this, you know, we can see these really clearly defined sections. Um, again, like I don't think colors are so important as much as it is to show the separation of the color, right? And I'll explain why later, but basically, we can clearly see that this is a section, this is a section, that's a section, and that's a section, blah blah blah.

[16:13] Now, you can always go in, if you want any one particular cue to be like a different color. Uh, you know, "I want, I want this to be purple." Save it, right? And you can give them names. So... you know what, names, you gotta be particularly anal to give your cues names, right? I don't. But hey, some people do. So we'll just call this "breakdown," right? Save that just for purposes, right? I'm not leaving anyone out here. And then we'll just call this like "drop 1". Okay. Save, cool.

[17:50] So, now we've got this track like set up. In the most basic sense. This button... this is fun. Uh, and this is something I noticed about CDJs that like, I just didn't like. Uh, because you have, you only had "only," which is usually plenty, you have eight cues. Uh, 1, 2, 3, 4, 5, 6, 7, 8. In Rekordbox, technically you have 16, but honestly, the players only have eight. Everybody only uses eight. Screw everybody.

[18:18] There was a thing where it's like, when I'm playing music, uh, more often than not, I play the track from the beginning sometimes. You know, especially if it's like one of mine that has an intro at the beginning, so it's like, "hey"... So I've made a, a kind of phantom cue that's just always gonna be there, and that's this one. It's a, it works, acts exactly like a cue button, except the only difference is it always plays from the grid start, so the beginning of where you start your grid.

[18:43] So that saves you from always needing to reserve cue 1 to be the beginning of your track, which I've just always found annoying. It's like, why am I wasting a cue? So now I can put, you know, cue 1 somewhere else. Like, not there. I gotta fix this zoom better so I can zoom out further, but... yeah.

[19:04] So, now when I hit cue 1, you know, I'm going to that other place. And then when I hit the phantom, it just play-from-start cue, it's always gonna play from position one. So it's like an extra, an extra cue that otherwise would prevent you from using cue 1 if you needed to start... so that's why I like that.

[19:24] Um, and then, lastly, let's just do this before we move on to the next little section, is, is like loop cues. Uh, so if I want to loop between bar 118 and 119, I'm just gonna, I'm gonna throw down a cue right here. Uh, I'm gonna color this something else in our palette, right? Um, and I'm gonna right-click it, and I'm gonna say "Loop," right? And I want to loop it... this is in beats. So, for four beats. So let's just call this "Loop," right?

[19:51] And now, what happened is, is it highlighted that section. But now I can just drag and modify the, the loop zone, right? In case you got it wrong when you were playing it back or whatever, right? So now when I hit that loop cue...

[20:07] [User demonstrates setting and playing a loop cue.]
But maybe we just want this. So we're gonna save that, right? So we're gonna hit save, done. Okay, so our track's done.

[20:24] Now if we go into Perform mode and we drag this track in, it's all set up. So, there's an option here... a couple options, right? So we've got like our scrolling waveform. Now, of course, if we, like I said, if we hide our browser and our master, we can see this like much better. And that's kind of what we're gonna focus on right now.

[20:47] Um, actually, I'm gonna, I'm gonna pause it here and then we'll just, um, I'll cut it, we'll go into the next section.

[20:58] Alright, so now we're at this little next part, right? And what I did, basically, to load that track in there is, uh, you know, you just drag it in there, just like you can drag it in here, whatever you want. Uh, and then to eject it, there's like a little eject button that I could probably design better, but whatever.

[21:22] Um, again, you know, uh, oh, I got the volume down, nice. So, here's our little fancy peak-holding volume meter for, for that bus on the mixer.

[21:42] So, you'll notice everything's running to that clock, right? So, uh, let me hide this master button. So, I'm quantized to quarter notes. So that just means it's gonna go every beat. But if I go to eighth, I can do... or sixteenth... whatever you like.

[22:21] Um, there's gonna be a "none" mode soon. Um, I just, I'm, I just have to, I just haven't implemented it yet. It's like, why work harder?

[22:36] Okay, so there's a waveform swap mode where we can show, you know, our scrolling waveform under, and then the big one over. Of course, we got our zoom... we can get right in there. I forgot... I got... I think this is good.

[22:58] So, hitting cues is doing exactly as you'd imagine. It's to the clock. Right? Just like you'd expect. Now, an interesting thing though is because these colors and these sections of the tracks that you've set up, uh, when you've authored things, is I've got this kind of option in here where it's like, you could do, uhm, clickable cue regions, right? So if I just hit save on this... and I'm clicking around here, nothing's happening, right?

[23:39] But if you go into, uh, I gotta clean this up too... Now when I just click on that region, it's gonna play from the start of that cue. So if I click, oh, on the orange section, it's like, it's like hitting the cue, but it's just more obvious. So if you know where the breakdown is visually in the track, you can just click on it. You can see the playhead there and how it's doing that. Okay, cool.

[24:20] And then of course, we have our other features that are just like you'd expect, right? Like... [demonstrates loop in, loop out, and beat jump controls] ...this. And that's, bang on the clock. And the reason it's so on is because it was authored properly. We set our grid up right. And that's all you really gotta do. And then it works like you'd... where you set your in and your out, and then it toggles it on.

[24:57] I've also added slip looping. So, the track is playing still underneath, and then when you let go... and of course you got... and that's dotted and triplet. I'll make little icons for that. And we've got bar and beat traversal. Uh, so especially with like, when you start traversing the bar here, you don't even notice it.

[25:50] Everything's groovy, it all locks, everything's good. So that's that. Uh, standard how-to-play-a-deck. So, um, I'm gonna go... let's, I'll just quickly run through this. Uh, we'll edit another track and then we'll just mix the two together. So I'm gonna, I'm gonna take this "Concentrate" track, right? So, in order to do that, again, we go into Edit mode, we go to "Concentrate". Oh, look at this, okay, we have an issue already, right? So, the grid's way off, right?

[27:30] So once again, it's, it's absolutely critical that you do this. Uh, you go in and you set your grid properly, right? So, I'm gonna stick my, my one downbeat grid there. Now the BPM detection was probably spot on, so we probably won't have to modify it. But in the case that you do, you can modify this. I, I just haven't done it yet. Um, and the beat detection, again, thanks to Steve Duda, is just, it's usually pretty spot on. But if it's not, you can go in and you can edit this.

[28:03] I just, whatever. It's, it could be a little better, a little tighter, I think. Yeah, see how it's a little earlier than when the transient hits? It's honestly a matter of flavor at this point of where you put that, how you line that up with your transient. But I like it like bang on, right? That's why I really wanted to, uh, you know, focus on the level of detail that we can get out of this waveform. And this is really fast and responsive.

[28:59] Um, because other grid editors from programs that will never, ever mention again for the rest of our lives, like, it's just too coarse. You can't see anything. And it's, it's just, I don't know, it's just useless. Okay, so again, there we go. We've set up a track. So let's just go... Yeah. Let's just throw a cue here. And we'll just throw in some arbitrary cues. Uh, and then one here. We'll only have two cues on this track. And because, again, I'm snapped to the grid, um, it's gonna put the cue there, right?

[29:41] So, I'm on quarter notes too. So if I go to eighth notes, that's fine too. Like you can, you can have your cues be in absolutely crazy spots. It's no problem about it. Just not off the grid until I, I forgot to add the "none" quantize, which means you could put the cue at like, you know, a really refined position. Um, but like I said, for techno, house, dubstep, drum and bass, whatever, it's just, everything's on the grid. Nobody cares. But there you go. So that's how it came out of Rekordbox. It just did the cues. I think it'll only fill up to eight, but, or 16 if you have 16 cues in Rekordbox.

[30:42] Oh, I forgot to explain. There's this, uh, little lock here too that prevents you from modifying, dragging, or accidentally making new cues. So I can't, I can't add new cues here, I can't delete them. I can only like, click on them and setting... It's basically like a, a cue lock feature so that you don't accidentally set new cues when you're playing and saving them. I'll figure it out.

[31:52] Now, let's talk about importing from other applications. Uh, so... okay, so now, you'll notice too, Rekordbox also, it also imported all my playlists, right? So if I've got "Test Playlist" and "Full Playlist," right? Um, and show playlists and all that stuff. I just don't have any show playlists on this machine, but yeah, it'll, it'll import your whole thing. Uh, it'll do your playlists, your artwork, and the order and stuff like that.

[32:21] But if you're absolutely, like, you know, adamant on having memorized everything you've done in the other application that shall not be named, we have this. Um, so in Rekordbox, you can export an XML, right? Uh, so you go to Rekordbox, I know I broke the rule, but, and then you go to File > Export Collection as XML. What that's gonna do is it's gonna give you all this dog shit in an XML file, which is fine. It's actually not dog shit, it's, it's the way I would've done it too, but...

[33:05] So, and you just hit okay. And that's it. That's all you gotta do. And now what it's doing, is it's importing the cues, the beat grids, the tracks, not the waveforms, because I am processing new waveforms, so we're not using Rekordbox's waveform stuff. Like, just, why? For what? So I'm gonna let that finish, and that just did 1,400 tracks. Like, it just imported my whole library. Done. I'm gonna save that.

[33:40] And now I'm gonna just refresh this 'cause I should have done that earlier. So there you go. I have all my cue points and everything for everything that I've set up in Rekordbox. So if like, if I go into Edit and I go into here, uh, Cthulhu Sleeps, right? And I look at the editor, and it's my beat grid...

[34:19] [Demonstrates playing a track imported from Rekordbox, with all cue points and names preserved.]
...and if we look at the names, you know... I've done, I've taken the time and I've done my whole library in Rekordbox. But now I don't have to do it again. You can just, it's here.

[34:33] There is one weird discrepancy though, um, which is sometimes... well, whatever. You know what, I'm not even gonna tell you 'cause it's an annoying bug. But, I do have an option in, and there will be utilities for doing really cool things, like I can quantize all the cue points, like by right-clicking here. I'm gonna make a little sub-editor menu for like utilities. Like, if you move the beat grid, move the cue points as well, blah blah blah.

[35:42] Catalog mode, uh, it's basically kind of a split view. It's, it's two browser, uh, sections, basically. So you can move from the lower to the top one, no problem. Um, so I've got this "Blah" folder for a playlist. I'm gonna create a new playlist, I'm gonna call this like, uh, "Test List", right? Okay, cool. So now we're in "Test List", but there's no playlist tracks in there, right? So I'm gonna go to "All Tracks" on the bottom and I'm gonna look up, "Hey, can I please strobe?" "Okay, yeah, well, I've got like 8,000 different versions of it. Which one do you want?" Uh, the Dimension remix. Let's go. Okay, so that's that.

[36:20] Um, and then we're gonna play the original Strobe. And then we're gonna go to Layton Giordani's remix. Uh, and then we're gonna play... this is gonna be the Strobe show. Uh, then we're gonna play the old 88 BPM intro. Uh, then we're gonna play the, the scored orchestral version, absolutely. So yeah, it's a, we're gonna play a whole show and it's just gonna be fucking Strobe. Amazing.

[36:42] Um, and then you can order it, you know, like you can. So you've got your, your order, right? So that's how that works, just like you would expect in a playlist. So now if I go back to like Perform mode and I go to Playlists here, and then I go to Test List, there's our Strobe set. So, you know, just like you'd expect, you could play our million versions of Strobe if we really want.

[37:53] [The user's DJ software appears to bug out briefly.]
Huh? What the fuck happened to Deck 2 here? That's so crazy. Anyway, um, I'll fix that. So, there you go. So that's how, uh, kind of, you know, Catalog mode is just kind of arranging and ordering and catalog, right? So if you wanna hear like, uh, the search function is like your friend, you know, down here. So like, "some chords", right? Oh, there you go. So I've got all these, whatever. Search function is your friend.

[38:20] Uh, yeah, and then there's that. Uh, so that's like your Rekordbox import. So, you know, if I go into Edit mode, I guess, and just look at any track, that's how it came out of Rekordbox. It just did the cues.

[39:20] And that pretty much covers the basics of like what you'd expect out of a DJ setup. But, I think the next few videos are gonna kind of roll into what is gonna make this just fucking awesome for actually performing and doing fun shit. Um, and including different technology and stuff like that. And then we'll talk about the Master section and routing and, and how it all makes sense. Um, and then we'll talk about the Master section, the mixer section we just explained with our meters and sending VST.

[52:11] Uh, we've got, uh, some internal stuff, which I'll probably finesse and make it a little bit better, but like... I've got isolator EQs...

[52:31] [The user demonstrates using the isolator EQs and a filter on a playing track.]
You know, what EQs do. I have to fix this, but it's cool. And a filter, 'cause you need filters, right? Again, if you were, if you were just routing to like a DJM, like a V10 or something, you wouldn't really need any of that because it has it all on it. So, whatever. But this is just for, I don't know, if you want to screw around. Or use the app natively and then sum the output and go.

[53:13] Alright, let's talk about, uh, VST inserts. This is great. Okay, so what we're gonna do is I'm gonna play the track here. Uh, oh, but before I do that, I'm gonna go into Settings and I'm gonna go into VST. Now, I've had some... we're gonna scan. It's gonna hiccup the application because you're not gonna be doing this, and don't you dare do this when you're playing live, uh, because it will disrupt the audio for like a split second.

[53:42] Um, so we've got VST instruments. It's just detected the shit ones that I have in here except for Serum. Uh, and then VST effects. Uh, so these are just gonna be like plugins and insert stuff. So, it'll list them all if you've got 100 million of them. I'll make this list a little nicer looking, but I've only got like, I only own two plugins, so... well... Uh, we'll do that.

[54:04] Okay, so now that we have those like selected in the list, I'm gonna select LFO Tool. So, let's play this, right? I'm gonna go to edit. And then it popped up on my other monitor. Um, that brings up the UI for LFO Tool. So, guess what? We side-chaining.

[54:34] [The user demonstrates using an LFO Tool VST plugin on a channel, showing how it's side-chaining the audio.]
Right? So... Okay, so now, what happened is, is it highlighted that section. But now I can just drag and modify the, the loop zone, right? In case you got it wrong when you were playing it back or whatever, right? So now when I hit that loop cue...

[54:37] Uh, so I can take that and then play this other track here, while the other track's side-chaining because it has that on it. Right? So, as you'd expect, it's an insert. Uh, but it's also taking the clock. And if you want to get really fancy... Oh, wait, isn't Serum an effect too? Hang on. Am I dumb?

[55:21] Yeah, but I didn't install it. So I am dumb. Yeah, any, any VST effect. But here's the fun part. And then we'll talk about... well, that's great that I can load LFO tool on here, but how do I control it, right? So we have up to eight macros for your four, uh, things, right?

[55:40] So, I'm gonna click on the word "Macro 1". It's gonna blink, which means it's waiting for you to move a parameter. So I'm gonna hit volume here. There. Now that macro's mapped. So...

[55:55] [The user maps a macro knob in the software to a parameter in the LFO Tool plugin.]
See what I'm saying? So, we don't need to see the plugin anymore. We can just... I wish I had more plugins then I could show you some cool shit, but... Uh, so let's add another LFO tool, except I'll just do the filter cutoff instead. Um, so imagine this is a reverb or something. Uh, so let's turn the filter on here. And then we'll edit, and then I'll go to Macro 2, I'll click that, it starts blinking, and I'll move the cutoff parameter.

[56:35] There we go. And then it names the parameter here. So, these are essentially two different VST plugins, but I've got my macros lined up like here. So it's, it's effectively just using VSTs as you would expect, as inserts, right? And you can do pre or post, which means, you know, if you know, right? That means like, so if the plugin is pre and it's a reverb and I move the fader up and down, the whole channel gain goes up and down. But if I put it on post and it's a reverb and I flick it like that, it'll send it to the reverb, but the reverb will tail out.

[57:25] So it's going to be really cool for like, if you want to flick in like delay effects. So if you got like an acapella but you want a certain word or something like that to get like, you know, delayed and stuff like that, you throw that on your insert and then just put it in post mode. So it's just the pre or post-fader is all that means. Uh, so yeah. There's our shit. Uh, I'm gonna stop this and then I'll just start it.

[58:32] Alright, we're back. I'm gonna light a cigarette again. I I always just do the things in the weirdest order here. Okay, so up in the header at the right, um, we have a, a button called Map Mode. And what this is gonna do... this is where it's gonna get really, really interesting, right?

[58:49] So I can say, let's map something. Oh, well, I have to hide that 'cause I can't. So I've got this, and I want to map all these, right? So I can map the beat, the loop, the, the out button, the in button, the one... I can map all these, and that's exactly what I'm gonna do right now. Is I'm gonna map these. So I'm just gonna go and then now when I hit the escape key, map mode's off, and now they're all mapped. And you can see my cursor change right there. And then I can go and... So there you go. Everything's mapped. So you don't even need to use the mouse to click this stuff if you've got this mapped to like a controller.

[59:36] And you can do that with everything on here. You can do it with the... you can map all these controls, you can map the solo, you can map the mute, the... you know, I think... yeah, that's just a UI element. But the EQ, the filter, the EQ on/off, all this stuff is all MIDI-mappable. And then, uh, we can just save it. And then we can load it.

[59:58] Oh, I forgot to mention, um, the MIDI is also OSC-compatible. So you can use an OSC controller, like TouchOSC, on your iPad or your phone and, and, and map all these controls to that. And so now you're literally just using this application as an engine. So, there's that.

[1:00:20] Let's go over to the Arrange player now. This is gonna be probably the last thing that I touch on because it's so in-depth and this is where it really gets fun. So now, if we go to Arrangements here, we've got, you know, you can add folders, but if we add a new arrangement, we'll just call it "Joel." Right? And now we can populate this with tracks, right? Now, if you notice, this is the first track we get. Now we can actually go and grab any track we want from here. Like I could just drag in "Strobe" and then drag in the other "Strobe" and drag in the other "Strobe." I'm not gonna do that because it would be a terrible mix.

[1:00:54] But what I am gonna do is, I'm gonna use the two tracks that we already kind of pre-authored here, which is "Serendipitous Connection" and "Iris". So I'm gonna take this, I'm gonna drag this in. And now this arrangement has no cues. It's just literally just the audio file. And now I'm gonna grab "Iris" and I'm gonna drag that in. And now we've got that, right? And so if I play it, [Track plays] it's just, it's just the track, right? You're like, "Well, so what? So you can just play two tracks. I can do that on Winamp, big whoop." Right?

[1:01:22] Yeah, you could, but can you do this? Um, now if I go to States, and this is where this whole concept of "Autopilot" comes into play, is you can save the state. So I'm gonna start with like, you know, the intro of this track, which will be "Iris." So I'm just gonna mute "Serendipitous Connection." And I'm gonna play it. And what I'm gonna do is I'm gonna just let this play for like, say, eight bars. And then I'm gonna add a state. What that just did is it just took a snapshot of everything on this mixer. It just said, "Okay, this one's muted, this one's playing at this volume, the EQs are set to this, the filters are set to this, this is the current tempo, blah blah blah blah blah." Okay? Now I can go, "Okay, that's cool," but I'm gonna play this.

[1:02:10] So I'm gonna unmute this track and now I'm gonna play it, and then I'm gonna fade this one in. So that, that's a new state. So I'm gonna go "Add State." Okay? Cool. So now I'm just gonna... now what I'm gonna do is I'm gonna play up to the breakdown here. And I'm gonna add another state. And then at the breakdown, I'm gonna filter this out and then add a state. Okay, cool.

[1:02:44] So now we've got four states in here. Now if I click on "Play," watch what happens. It's now going to play through all those states. And you can see the playhead move through the states. It's just gonna do exactly what I did. And you can just sit here and just create these perfect studio-quality mixes using just all your tracks and they all sync up to the clock. And you can change the tempo on the fly because it's a constant running clock. It's not stopping a transport and restarting a transport and changing tempo. Everything just kind of goes. And it's so tight. It's so fucking tight. I mean, it's not like that right now because I'm not really trying, but you know what I mean.

[1:03:31] And then you can just do your little filter sweep. And that's that. So it's gonna keep going through these. And then I can just hit "Stop." Uh, so, I don't know, for the sake of this, let's just make it really simple. Let's just, uh, add a state there, and then we're gonna add another state there. Um, and then now if I want this state to be only like, say, I don't know, a quarter of a beat, or a bar, sorry, it's gonna, you know, do that.

[1:04:03] We could just set our states up like so. And what this is gonna do is it's just gonna like, jump back and forth. You could do some really crazy, cool glitchy shit with this too. Right? And it's all perfectly in time. All the time. And then you can go up and down and like, whatever. But now, what I can do is, now that I have this arrangement, I can save this arrangement. Let's call this "Joel". Okay, cool.

[1:04:36] Now let's just go, um, I'm gonna remove that. And I'm gonna start a new arrangement. So I'm just gonna go here, go, and just, I don't know, whatever. Cool. Now I've got this new blank arrangement, nothing going on, but I can drag this in here. What this is now is this is the arrangement that I made before, rendered out into its own waveform. Right?

[1:05:01] So I can now take this and I can treat this as its own track now. And then I could do a state here, and then, you know, I could have like another track going over here. And it will all just seamlessly play into each other. And you can stack these arrangements and make these like, huge, big, hour-long sets if you want, and they all just play and they're all synced and they're all perfect. Um, so that's that. That's, I, I really, I mean, it's not done yet, but that's like, that's what's gonna make this thing fucking sick. I love it.

[1:05:36] So now if I go to perform, and then I'm gonna play that arrangement. Right? So there's our two tracks playing at the same time. Right? It's just doing its thing. It's going through its little states. And then I can, you know, I can grab another track. Um, say "Strobe," which is at 128 BPM. And then I'll just, you know, do its thing. Right?

[1:06:05] And I can just play, like... you know, it's like a, like a live performance tool, which I always just really, really wanted. Um, and it just works great. So there's gonna be a lot more to add into this, but that's the basics of it. And then, you know, you can do all the stuff that you normally do, right? So... that's it. That's all I wanted to show you. It's gonna be great. It's gonna be fun. I'm having a lot of fun with it. So, talk to you later. Bye.
