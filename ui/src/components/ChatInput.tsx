import React, { useState, useRef, useCallback, useEffect } from "react";

interface ChatInputProps {
  onSend: (content: string, imageUrls?: string[]) => void;
  onInterrupt: () => void;
  streaming: boolean;
  disabled?: boolean;
}

export function ChatInput({
  onSend,
  onInterrupt,
  streaming,
  disabled,
}: ChatInputProps) {
  const [text, setText] = useState("");
  const [imageUrls, setImageUrls] = useState<string[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed && imageUrls.length === 0) return;
    onSend(trimmed, imageUrls.length > 0 ? imageUrls : undefined);
    setText("");
    setImageUrls([]);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }, [text, imageUrls, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (streaming) {
          onInterrupt();
        } else {
          handleSubmit();
        }
      }
    },
    [handleSubmit, streaming, onInterrupt]
  );

  const handleInput = useCallback(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
    }
  }, []);

  // Helper: read a file as data URL and add to images
  const addImageFromFile = useCallback((file: File) => {
    if (!file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => {
      setImageUrls((prev) => [...prev, reader.result as string]);
    };
    reader.readAsDataURL(file);
  }, []);

  // Helper: add image from clipboard
  const addImageFromBlob = useCallback((blob: Blob) => {
    const reader = new FileReader();
    reader.onload = () => {
      setImageUrls((prev) => [...prev, reader.result as string]);
    };
    reader.readAsDataURL(blob);
  }, []);

  const removeImage = useCallback((index: number) => {
    setImageUrls((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Handle Ctrl+V / Cmd+V paste — detect images on clipboard
  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;

      for (let i = 0; i < items.length; i++) {
        if (items[i].type.startsWith("image/")) {
          e.preventDefault();
          const blob = items[i].getAsFile();
          if (blob) addImageFromBlob(blob);
          return;
        }
      }
      // Text paste — let default behavior handle it
    },
    [addImageFromBlob]
  );

  // Handle file input change (from file picker) — supports multiple
  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (!files) return;
      for (let i = 0; i < files.length; i++) {
        addImageFromFile(files[i]);
      }
      // Reset so the same files can be re-selected
      e.target.value = "";
    },
    [addImageFromFile]
  );

  // Handle drag-and-drop — supports multiple files
  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (!files) return;
      for (let i = 0; i < files.length; i++) {
        addImageFromFile(files[i]);
      }
    },
    [addImageFromFile]
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const canSend = (text.trim() || imageUrls.length > 0) && !disabled;

  return (
    <div className="chat-input-area">
      <div
        className="chat-input-card"
        ref={containerRef}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
      >
        {/* Image previews */}
        {imageUrls.length > 0 && (
          <div className="chat-images-preview">
            {imageUrls.map((url, i) => (
              <div key={i} className="chat-image-preview">
                <img src={url} alt={`Attachment ${i + 1}`} className="chat-image-thumb" />
                <button
                  className="chat-image-remove"
                  onClick={() => removeImage(i)}
                  title="Remove image"
                >
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={textareaRef}
          className="chat-textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onInput={handleInput}
          placeholder={
            streaming
              ? "Agent is working\u2026"
              : imageUrls.length > 0
              ? "Add a message (optional)\u2026"
              : "Ask anything\u2026 (Ctrl+V to paste image)"
          }
          rows={1}
          disabled={disabled}
        />
        <div className="chat-input-bottom">
          <div className="chat-input-left">
            <button
              className="chat-icon-btn"
              onClick={() => fileInputRef.current?.click()}
              title="Attach image"
              disabled={disabled}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <rect x="3" y="3" width="18" height="18" rx="3" stroke="currentColor" strokeWidth="1.5" />
                <circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" />
                <path d="M3 16l5-5 4 4 3-3 6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              style={{ display: "none" }}
              onChange={handleFileChange}
            />
          </div>
          <div className="chat-input-right">
            {streaming ? (
              <button
                className="chat-stop-btn"
                onClick={onInterrupt}
                title="Stop generation"
              >
                <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                  <rect x="3" y="3" width="10" height="10" rx="2" />
                </svg>
              </button>
            ) : (
              <button
                className="chat-send-btn"
                onClick={handleSubmit}
                disabled={!canSend}
                title="Send message"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                  <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
