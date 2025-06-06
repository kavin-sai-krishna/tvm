/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

/*!
 * \file rpc_channel.cc
 */
#include "rpc_channel.h"

#include <tvm/runtime/logging.h>

#include <string>

namespace tvm {
namespace runtime {

size_t CallbackChannel::Send(const void* data, size_t size) {
  TVMFFIByteArray bytes;
  bytes.data = static_cast<const char*>(data);
  bytes.size = size;
  int64_t n = fsend_(&bytes).cast<int64_t>();
  if (n == -1) {
    LOG(FATAL) << "CallbackChannel::Send";
  }
  return static_cast<size_t>(n);
}

size_t CallbackChannel::Recv(void* data, size_t size) {
  Any ret = frecv_(size);

  auto opt_bytes = ret.try_cast<ffi::Bytes>();
  CHECK(opt_bytes.has_value()) << "CallbackChannel::Recv";

  ffi::Bytes bytes = std::move(opt_bytes.value());
  memcpy(static_cast<char*>(data), bytes.data(), bytes.size());
  return bytes.size();
}

}  // namespace runtime
}  // namespace tvm
