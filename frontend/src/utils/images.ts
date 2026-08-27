// Read a File as a base64 data URL, for image attachments (send and message
// edit both upload this way; the backend stores the bytes content-addressed).
export function readFileAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}
