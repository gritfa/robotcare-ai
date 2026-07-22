export interface AttachmentUploadQueueResult<T> {
  uploaded: T[]
  pending: File[]
  error?: unknown
}

/** Stop at the first failure so a 429 never causes additional quota-consuming calls. */
export async function uploadAttachmentQueue<T>(
  files: File[],
  upload: (file: File) => Promise<T>,
): Promise<AttachmentUploadQueueResult<T>> {
  const uploaded: T[] = []
  for (let index = 0; index < files.length; index += 1) {
    try {
      uploaded.push(await upload(files[index]))
    } catch (error) {
      return { uploaded, pending: files.slice(index), error }
    }
  }
  return { uploaded, pending: [] }
}
