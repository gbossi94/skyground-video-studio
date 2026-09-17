-- Skyground → DaVinci Resolve, the half that runs inside Resolve.
--
-- Resolve lists this file under Workspace → Scripts → Skyground (it scans
-- its Scripts folders once, at startup). It reads the manifest that
-- `python3 Skyground.py` wrote in ~/Movies/Skyground/latest.lua and builds,
-- with Resolve's own API, a project of the cut's name with a timeline of
-- the canvas size and frame rate: one clip per clip of the cut, frame-exact
-- on the raw footage, plus the captions as a subtitle track. Nothing is
-- rendered: from here the hand decides.
--
-- Lua because Resolve's Lua always runs, while its Python needs an
-- interpreter Resolve accepts, and some Macs have none. The Lua here is
-- Resolve's sandbox: no io, no require, no os.execute — only files a path
-- reaches, and the scripting objects `resolve` and `project`.
--
-- What it says goes to Workspace → Console; what fails goes there and to
-- ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/logs/ResolveDebug.txt.

local function say(message)
  print("Skyground: " .. message)
end

local function round(x)
  return math.floor(x + 0.5)
end

-- Each clip of the cut as startFrame/endFrame *of the raw footage*, at the
-- raw footage's own frame rate, which is what Resolve wants; the length is
-- the whole number of timeline frames the studio already decided on.
-- Resolve 21 reads endFrame as exclusive: 166 frames from frame 135 is
-- {135, 301}; {135, 300} landed 165 on the timeline.
local function clip_ranges(clips, clip_fps, fps)
  local ranges = {}
  for _, clip in ipairs(clips) do
    local length = clip.frames / fps
    local first = round(clip.start * clip_fps)
    ranges[#ranges + 1] = {first, first + math.max(1, round(length * clip_fps))}
  end
  return ranges
end

local function build(resolve, cut)
  local manager = resolve:GetProjectManager()
  local project = manager:LoadProject(cut.name) or manager:CreateProject(cut.name)
  if not project then
    error("Resolve non ha creato né aperto il progetto «" .. cut.name .. "»")
  end
  -- Before any media: the frame rate is locked once the pool has a clip.
  -- (timelinePlaybackFrameRate is read-only and follows this one.)
  for _, setting in ipairs({
    {"timelineFrameRate", tostring(cut.fps)},
    {"timelineResolutionWidth", tostring(cut.width)},
    {"timelineResolutionHeight", tostring(cut.height)},
  }) do
    if not project:SetSetting(setting[1], setting[2]) then
      say("Resolve ha rifiutato " .. setting[1] .. " = " .. setting[2])
    end
  end

  local pool = project:GetMediaPool()
  local items = resolve:GetMediaStorage():AddItemListToMediaPool({cut.raw}) or {}
  local raw_item = items[1]
  if not raw_item then
    error("Resolve non ha importato il girato " .. cut.raw)
  end
  local clip_fps = tonumber(raw_item:GetClipProperty("FPS")) or cut.fps

  local title = "Skyground"
  local timeline = pool:CreateEmptyTimeline(title)
  local number = 2
  while not timeline do -- exists already: a numbered one, never over the old
    timeline = pool:CreateEmptyTimeline(title .. " " .. number)
    number = number + 1
  end
  local infos = {}
  for _, range in ipairs(clip_ranges(cut.clips, clip_fps, cut.fps)) do
    infos[#infos + 1] = {mediaPoolItem = raw_item, startFrame = range[1], endFrame = range[2]}
  end
  local appended = pool:AppendToTimeline(infos) or {}
  if #appended ~= #infos then
    say("Resolve ha messo " .. #appended .. " clip su " .. #infos)
  end

  -- The captions: the SRT goes into the pool as a Subtitle clip, and lands
  -- on the timeline only once a subtitle track exists to receive it.
  local subtitles = "no"
  local ok, err = pcall(function()
    local srt_items = pool:ImportMedia({cut.subtitles}) or {}
    if srt_items[1] and timeline:AddTrack("subtitle") and pool:AppendToTimeline(srt_items) then
      subtitles = #(timeline:GetItemListInTrack("subtitle", 1) or {}) .. " battute"
    end
  end)
  if not ok then
    say("sottotitoli non importati (" .. tostring(err) .. "); importali da File → Import → Subtitle")
  end
  return {timeline = timeline:GetName(), clips = #appended, clip_fps = clip_fps, subtitles = subtitles}
end

local function main()
  local home = os.getenv("HOME")
  local manifest = home .. "/Movies/Skyground/latest.lua"
  local load = loadfile(manifest)
  if not load then
    error("manca " .. manifest .. ": prima, in un terminale, python3 Skyground.py")
  end
  local cut = load()
  say("progetto «" .. cut.name .. "», " .. #cut.clips .. " clip, girato " .. cut.raw)
  local result = build(resolve, cut)
  say("timeline «" .. result.timeline .. "» con " .. result.clips .. " clip a " .. result.clip_fps .. " fps; sottotitoli: " .. result.subtitles)
end

main()
