hs.hotkey.bind({"cmd", "ctrl"}, "f", function()
  local win = hs.window.focusedWindow()
  if win then
    win:setFullscreen(not win:isFullscreen())
  end
end)

-- These paths are supplied by the Home Manager declaration.
local importer = @photosImportApp@
local pngpaste = @pngpaste@

local function importToPhotos(tmpFile)
  hs.task.new("/usr/bin/open", function(exitCode, _, stderr)
    if exitCode ~= 0 then
      hs.notify.new({
        title = "Screenshot",
        informativeText = "Photos import failed: " .. (stderr or ("exit code " .. exitCode)),
      }):send()
    end
    os.remove(tmpFile)
  end, {"-W", "-n", importer, "--args", tmpFile}):start()
end

local function saveClipboardAndImport()
  local tmpFile = os.tmpname() .. ".png"
  hs.task.new(pngpaste, function(exitCode, _, stderr)
    if exitCode ~= 0 then
      os.remove(tmpFile)
      hs.notify.new({
        title = "Screenshot",
        informativeText = "Could not save the screenshot: " .. (stderr or ("exit code " .. exitCode)),
      }):send()
      return
    end
    importToPhotos(tmpFile)
  end, {tmpFile}):start()
end

local function captureToClipboard(args)
  hs.task.new("/usr/sbin/screencapture", function(exitCode, _, stderr)
    if exitCode ~= 0 then
      -- Interactive capture returns nonzero when the user presses Escape.
      if stderr and stderr ~= "" then
        hs.notify.new({
          title = "Screenshot",
          informativeText = "Screen capture failed: " .. stderr,
        }):send()
      end
      return
    end
    saveClipboardAndImport()
  end, args):start()
end

hs.hotkey.bind({"cmd", "shift"}, "3", function()
  captureToClipboard({"-c", "-m", "-x"})
end)

hs.hotkey.bind({"cmd", "shift"}, "4", function()
  -- Let macOS own the image buffer; Hammerspoon only coordinates the processes.
  captureToClipboard({"-i", "-c", "-x"})
end)
