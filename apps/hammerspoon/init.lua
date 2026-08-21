hs.hotkey.bind({"cmd", "ctrl"}, "f", function()
  local win = hs.window.focusedWindow()
  if win then
    win:setFullscreen(not win:isFullscreen())
  end
end)

hs.hotkey.bind({"cmd", "shift"}, "3", function()
  local image = hs.screen.mainScreen():snapshot()
  hs.pasteboard.writeObjects(image)

  local tmpFile = os.tmpname() .. ".png"
  image:saveToFile(tmpFile)

  local importer = os.getenv("HOME") .. "/.config/nix/bin/photos-import.app"
  hs.task.new("/usr/bin/open", function(exitCode, _, stderr)
    if exitCode ~= 0 then
      hs.notify.new({title="Screenshot", informativeText="Photos import failed: " .. stderr}):send()
    end
    os.remove(tmpFile)
  end, {"-W", "-n", importer, "--args", tmpFile}):start()
end)

local function importToPhotos(tmpFile)
  local importer = os.getenv("HOME") .. "/.config/nix/bin/photos-import.app"
  hs.task.new("/usr/bin/open", function(exitCode, _, stderr)
    if exitCode ~= 0 then
      hs.notify.new({title="Screenshot", informativeText="Photos import failed: " .. stderr}):send()
    end
    os.remove(tmpFile)
  end, {"-W", "-n", importer, "--args", tmpFile}):start()
end

hs.hotkey.bind({"cmd", "shift"}, "4", function()
  local tmpFile = os.tmpname() .. ".png"
  -- Launch native macOS region-selection UI; exits when user finishes (or cancels)
  hs.task.new("/usr/sbin/screencapture", function(exitCode)
    if exitCode ~= 0 then return end  -- user pressed Escape
    local image = hs.image.imageFromPath(tmpFile)
    if not image then return end
    hs.pasteboard.writeObjects(image)
    importToPhotos(tmpFile)
  end, {"-i", tmpFile}):start()
end)
